"""Cached directional pre-move lifecycle and causal, comparable rank migration.

This consumes production engine observations; it never downloads candles,
reweights the pressure score or changes the existing completed-1m trigger.
Ranks describe this observed eligible cohort, not the entire crypto market.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime

from pydantic import Field, model_validator

from .discovery_ranking import _midranks
from .models import Model, aware


class PreMoveSettings(Model):
    radar_score: float = Field(default=25, ge=0, le=100)
    watch_score: float = Field(default=40, ge=0, le=100)
    pre_ignition_score: float = Field(default=55, ge=0, le=100)
    loaded_score: float = Field(default=65, ge=0, le=100)
    imminent_atr: float = Field(default=.2, ge=0)
    proximity_atr: float = Field(default=.75, gt=0)
    minimum_compression: float = Field(default=.35, ge=0, le=1)
    minimum_structure: float = Field(default=.1, ge=0, le=1)
    minimum_tests: int = Field(default=2, ge=1)
    minimum_taker_pressure: float = Field(default=.52, ge=.5, le=1)
    fast_rank_velocity: float = Field(default=.5, gt=0)
    fast_rank_acceleration: float = Field(default=.1, gt=0)
    stale_seconds: float = Field(default=90, gt=0)
    maximum_rank_gap_seconds: float = Field(default=180, ge=60)
    history_minutes: int = Field(default=60, ge=3, le=10080)

    @model_validator(mode="after")
    def ordered(self):
        if not self.radar_score <= self.watch_score <= self.pre_ignition_score <= self.loaded_score:
            raise ValueError("pre-move score thresholds must be ordered")
        if self.imminent_atr > self.proximity_atr:
            raise ValueError("imminent distance must be inside configured proximity")
        return self


def _time(value):
    return aware(value if isinstance(value, datetime) else datetime.fromisoformat(value))


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def conditional_pre_move_path(bias, trigger, invalidation, targets):
    """Same conditional structure semantics as War Room; no future OHLC path."""
    if trigger is None:
        return []
    crossing = "above" if bias == "LONG" else "below"
    return [
        {"step": 1, "state": "CLOSED_1M_TRIGGER_REVIEW", "level": trigger,
         "condition": f"IF a completed 1m close is {crossing} the predefined trigger plus the existing directional ATR/tick buffer, THEN review the native trigger. An open candle or wick is insufficient."},
        {"step": 2, "state": "5M_ACCEPTANCE_REVIEW", "level": trigger,
         "condition": "IF subsequent completed 5m candles hold the former boundary with intact structure, THEN review acceptance; this never delays the initial 1m confirmation."},
        {"step": 3, "state": "CONTINUATION_OR_FAILURE_REVIEW", "level": targets[0] if targets else None,
         "invalidation": invalidation,
         "condition": "IF later 15m structure accepts the break, THEN review continuation toward available observed levels. IF controlling invalidation is lost, THEN cancel this thesis; no opposite trade is implied."},
    ]


class PreMoveRanker:
    def __init__(self, settings: PreMoveSettings | None = None):
        self.settings = settings or PreMoveSettings()
        self._samples: list[dict] = []
        self._states: dict[str, dict] = {}
        self._observed: dict[str, str] = {}
        self._restart: dict[str, str] = {}
        self._last_update: datetime | None = None

    def export_state(self, *, maximum_samples: int | None = None):
        if maximum_samples is not None and maximum_samples < 3:
            raise ValueError("at least three rank samples are required for a restart checkpoint")
        samples = self._samples if maximum_samples is None else self._samples[-maximum_samples:]
        return copy.deepcopy({"version": 1, "settings": self.settings.model_dump(mode="json"),
            "samples": samples, "states": self._states, "observed": self._observed,
            "last_update": self._last_update.isoformat() if self._last_update else None})

    def checkpoint_state(self):
        """Compact restart checkpoint; detailed evidence lives in its own archive."""
        return self.export_state(maximum_samples=3)

    def restore_state(self, payload):
        if not payload or payload.get("version") != 1 or payload.get("settings") != self.settings.model_dump(mode="json"):
            return
        samples = copy.deepcopy(payload.get("samples", []))[-self.settings.history_minutes:]
        prior = None
        for sample in samples:
            when = _time(sample["as_of"])
            if prior is not None and int(when.timestamp() // 60) <= int(prior.timestamp() // 60):
                raise ValueError("rank samples must be chronological and minute-distinct")
            prior = when
        self._samples = samples
        self._states = copy.deepcopy(payload.get("states", {}))
        self._observed = copy.deepcopy(payload.get("observed", {}))
        self._restart = dict(self._observed)
        self._last_update = _time(payload["last_update"]) if payload.get("last_update") else None

    def _row(self, asset_id, observation, bias, now):
        direction = observation.get("directions", {}).get(bias) or {}
        metrics = copy.deepcopy(direction.get("metrics", {}))
        score = _number(direction.get("score"))
        level = _number(direction.get("level"))
        price = _number(observation.get("price"))
        distance = _number(direction.get("distance_atr"))
        quality = dict(observation.get("data_quality", {}))
        reason, status = "Observed causal directional pressure", "AVAILABLE"
        try:
            observed = _time(observation["as_of"])
            discovered = _time(direction["level_discovered_at"]) if direction.get("level_discovered_at") else None
        except (TypeError, ValueError, KeyError):
            observed, discovered = None, None
        if observed is None or observed > now or discovered is None or discovered > observed:
            status, reason = "UNAVAILABLE", "Missing or future decision/trigger discovery timestamp"
        elif asset_id in self._restart and observed <= _time(self._restart[asset_id]):
            status, reason = "STALE", "Restored state requires a newer market observation"
        elif (now-observed).total_seconds() > self.settings.stale_seconds or any(quality.get(frame) == "STALE" for frame in ("1m", "5m")):
            status, reason = "STALE", "Critical market observation is stale"
        elif any(quality.get(frame) != "AVAILABLE" for frame in ("1m", "5m")):
            status, reason = "UNAVAILABLE", "Fresh native 1m/5m evidence is required"
        elif observation.get("status") != "EVALUATED" or score is None or not 0 <= score <= 100 or level is None or level <= 0 or price is None or price <= 0 or distance is None:
            status, reason = "UNAVAILABLE", "Insufficient directional pressure evidence"
        if observation.get("status") == "LATE/MISSED":
            reason = "Native engine marks this move as late/missed; no new pre-move claim"
        return {"asset_id": asset_id, "symbol": observation.get("symbol", asset_id), "bias": bias,
            "as_of": now.isoformat(), "observation_timestamp": observed.isoformat() if observed else None,
            "trigger_discovered_at": discovered.isoformat() if discovered else None,
            "state": "NORMAL", "status": status, "score": score,
            "components": copy.deepcopy(direction.get("components", {})), "metrics": metrics,
            "price": price, "trigger": level, "distance_atr": distance,
            "invalidation": None, "targets": [], "data_quality": quality,
            "reason": reason, "calibration": "UNCALIBRATED_SCORE_NOT_PROBABILITY", "probability": None,
            "rank": {"current_rank": None, "previous_rank": None, "rank_delta": None,
                "rank_velocity": None, "rank_acceleration": None, "cohort_size": 0,
                "basis": "CURRENT_DIRECTIONAL_CONTINUATION_PRESSURE_IN_OBSERVED_COHORT",
                "velocity_unit": "rank_positions_per_minute", "acceleration_unit": "rank_positions_per_minute_squared",
                "reason": "No eligible comparable cross-section"}, "execution_enabled": False}

    @staticmethod
    def _signature(rows):
        return {row["asset_id"]: sorted((name, part.get("weight"), part.get("normalized") is not None)
                for name, part in row["components"].items() if isinstance(part, dict)) for row in rows}

    def _ranks(self, rows, now):
        sample = {"as_of": now.isoformat(), "cohorts": {}, "ranks": {}, "signatures": {}, "observations": {}}
        for bias in ("LONG", "SHORT"):
            group = [row for row in rows if row["bias"] == bias and row["status"] == "AVAILABLE"]
            ranks = _midranks({row["asset_id"]: row["score"] for row in group}) if len(group) >= 2 else {}
            cohort = sorted(ranks)
            signature = _digest(self._signature(group))
            cohort_signature = _digest(cohort)
            sample["cohorts"][bias], sample["ranks"][bias], sample["signatures"][bias] = cohort_signature, ranks, signature
            sample["observations"][bias] = _digest({row["asset_id"]: row["observation_timestamp"] for row in group})
            priors = [entry for entry in self._samples if int(_time(entry["as_of"]).timestamp() // 60) < int(now.timestamp() // 60)]
            previous = priors[-1] if priors else None
            dt = (now-_time(previous["as_of"])).total_seconds()/60 if previous else None
            comparable = bool(previous and cohort and previous["cohorts"].get(bias) == cohort_signature
                and previous["signatures"].get(bias) == signature and dt > 0
                and dt*60 <= self.settings.maximum_rank_gap_seconds
                and previous["observations"].get(bias) != sample["observations"][bias])
            for row in group:
                identity, rank = row["asset_id"], row["rank"]
                rank.update(current_rank=ranks.get(identity), cohort_size=len(group))
                rank["reason"] = "First eligible minute observation; migration unavailable" if not previous else "Rank migration unavailable: cohort, evidence, timestamp or observation changed incompatibly"
                if len(group) < 2:
                    rank["reason"] = "At least two fresh comparable directional observations are required"
                if not comparable:
                    continue
                old = previous["ranks"][bias][identity]
                velocity = (old-ranks[identity])/dt
                rank.update(previous_rank=old, previous_timestamp=previous["as_of"], rank_delta=old-ranks[identity],
                            rank_velocity=velocity, reason="Same eligible directional cohort and evidence; positive values mean improving rank")
                if len(priors) < 2:
                    continue
                before = priors[-2]
                elapsed = (_time(previous["as_of"])-_time(before["as_of"])).total_seconds()/60
                if (before["cohorts"].get(bias) == cohort_signature and before["signatures"].get(bias) == signature
                        and 0 < elapsed*60 <= self.settings.maximum_rank_gap_seconds
                        and before["observations"].get(bias) != previous["observations"].get(bias)):
                    prior_velocity = (before["ranks"][bias][identity]-old)/elapsed
                    rank["rank_acceleration"] = (velocity-prior_velocity)/dt
        # Sampling polling alone cannot manufacture observations or rank history.
        if (any(sample["ranks"].values()) and (not self._samples or
                int(now.timestamp()//60) > int(_time(self._samples[-1]["as_of"]).timestamp()//60)
                and sample["observations"] != self._samples[-1]["observations"])):
            self._samples.append(sample)
            self._samples = self._samples[-self.settings.history_minutes:]

    def _lifecycle(self, row, observation, episodes, now):
        bias, asset_id = row["bias"], row["asset_id"]
        candidates = [episode for episode in episodes.values() if isinstance(episode, dict)
                      and episode.get("asset_id") == asset_id and episode.get("bias") == bias
                      and episode.get("discovered_at") and _time(episode["discovered_at"]) <= now]
        def episode_priority(value):
            active = value.get("status") == "ACTIVE"
            return (2 if active and value.get("phase") == "BREAK_CONFIRMED" else
                    1 if active and value.get("setup_type", "CONTINUATION") == "CONTINUATION" else 0,
                    _time(value["discovered_at"]))
        episode = max(candidates, key=episode_priority, default=None)
        if episode:
            row.update(episode_id=episode.get("episode_id"), setup_type=episode.get("setup_type", "CONTINUATION"),
                       invalidation=_number(episode.get("invalidation")),
                       targets=[price for raw in episode.get("targets", []) if (price := _number(raw)) is not None and price > 0])
            known_trigger, atr = _number(episode.get("trigger")), _number(episode.get("atr"))
            if (episode.get("status") == "ACTIVE" and episode.get("setup_type", "CONTINUATION") == "CONTINUATION"
                    and known_trigger is not None and atr is not None and atr > 0 and row["price"] is not None):
                row.update(trigger=known_trigger, trigger_discovered_at=episode["discovered_at"],
                           distance_atr=(1 if bias == "LONG" else -1)*(known_trigger-row["price"])/atr)
        # After a native confirmed break there may be no new pre-break candidate
        # level. Its absence cannot erase an otherwise fresh native confirmation.
        observed = _time(row["observation_timestamp"]) if row["observation_timestamp"] else None
        native_fresh = (observed is not None and observed <= now
            and (now-observed).total_seconds() <= self.settings.stale_seconds
            and observation.get("status") == "EVALUATED"
            and all(row["data_quality"].get(frame) == "AVAILABLE" for frame in ("1m", "5m")))
        if (row["status"] == "UNAVAILABLE" and native_fresh and episode
                and episode.get("phase") in {"BREAK_CONFIRMED", "INVALIDATED"}):
            row["status"] = "AVAILABLE"
        if row["status"] != "AVAILABLE" or episode and episode.get("restart_stale"):
            if episode and episode.get("restart_stale"):
                row.update(status="STALE", reason="Restored trigger episode awaits fresh completed market evidence")
            if observation.get("status") == "LATE/MISSED":
                row["state"] = "EXHAUSTION"
            return
        if episode and episode.get("status") == "INVALIDATED":
            row.update(state="FAILED", reason="Native engine invalidated the known setup")
            return
        if episode and episode.get("status") in {"LATE", "MISSED"}:
            row.update(state="EXHAUSTION", reason="Native engine marks the setup late/missed; no early claim")
            return
        if episode and episode.get("phase") == "BREAK_CONFIRMED" and episode.get("status") == "ACTIVE":
            row["trigger"] = _number(episode.get("trigger"))
            row["trigger_discovered_at"] = episode["discovered_at"]
            confirmed, last = episode.get("confirmed_at"), episode.get("last_1m")
            if confirmed and _time(confirmed) > now or last and _time(last) > now:
                row.update(status="UNAVAILABLE", reason="Future native confirmation cannot enter this decision")
                return
            row.update(state="ACTIVE" if confirmed and last and _time(last) > _time(confirmed) else "TRIGGER",
                       reason="Native completed-1m engine confirmation; this ranker does not confirm open candles")
            if episode.get("setup_type", "CONTINUATION") != "CONTINUATION":
                row.update(score=_number(episode.get("score")), components=copy.deepcopy(episode.get("components", {})))
            return
        distance, metrics = row["distance_atr"], row["metrics"]
        compression = _number(metrics.get("range_compression"))
        structure = _number(metrics.get("lower_high_strength" if bias == "SHORT" else "higher_low_strength"))
        tests = _number(metrics.get("support_test_count" if bias == "SHORT" else "resistance_test_count"))
        native = _number(metrics.get("taker_buy_ratio"))
        flow = 1-native if bias == "SHORT" and native is not None else native
        pre_trigger = (row["price"] is not None and row["trigger"] is not None
                       and (1 if bias == "LONG" else -1)*(row["trigger"]-row["price"]) >= 0)
        readiness = (pre_trigger and 0 <= distance <= self.settings.proximity_atr and compression is not None
            and compression >= self.settings.minimum_compression and structure is not None
            and structure >= self.settings.minimum_structure and tests is not None and tests >= self.settings.minimum_tests)
        if not readiness:
            row["reason"] = "Pre-trigger proximity, compression and repeated directional structure are not jointly established"
            if not pre_trigger or distance < 0:
                row["reason"] = "Live price crossed the level; wait for the authoritative completed-1m confirmation"
            return
        velocity, acceleration = row["rank"]["rank_velocity"], row["rank"]["rank_acceleration"]
        climber = velocity is not None and velocity > 0 and (velocity >= self.settings.fast_rank_velocity
            or acceleration is not None and acceleration >= self.settings.fast_rank_acceleration)
        if row["score"] >= self.settings.radar_score or climber:
            row.update(state="RADAR", reason="Measured compressed pre-trigger structure; rapid comparable rank improvement" if climber else "Measured compressed pre-trigger structure")
        if flow is None or not 0 <= flow <= 1 or flow < self.settings.minimum_taker_pressure:
            row["reason"] += "; supportive native directional taker activity unavailable/insufficient; no advanced stage"
            return
        for threshold, state in ((self.settings.watch_score, "WATCH"), (self.settings.pre_ignition_score, "PRE_IGNITION"), (self.settings.loaded_score, "LOADED")):
            if row["score"] >= threshold:
                row.update(state=state, reason="Predefined trigger remains unbroken; measured compression, directional structure and native flow align")
        if row["state"] == "LOADED" and distance <= self.settings.imminent_atr:
            row.update(state="TRIGGER_IMMINENT", reason="Loaded measured setup is inside the configured ATR proximity; no breakout is yet confirmed")

    def update(self, observations: dict, episodes: dict, as_of: datetime) -> list[dict]:
        now = aware(as_of)
        if self._last_update is not None and now < self._last_update:
            raise ValueError("out-of-order pre-move decision timestamp")
        episodes = episodes.get("episodes", {}) if "episodes" in episodes else episodes
        episode_groups = {}
        for key, episode in episodes.items():
            if isinstance(episode, dict):
                pair = (episode.get("asset_id"), episode.get("bias"))
                episode_groups.setdefault(pair, {})[key] = episode
        rows = []
        for asset_id, observation in sorted(observations.items()):
            if not isinstance(observation, dict):
                continue
            rows.extend(self._row(asset_id, observation, bias, now) for bias in ("LONG", "SHORT"))
        self._ranks(rows, now)
        for row in rows:
            self._lifecycle(row, observations[row["asset_id"]],
                            episode_groups.get((row["asset_id"], row["bias"]), {}), now)
            key = f"{row['asset_id']}|{row['bias']}"
            prior = self._states.get(key)
            changed = prior is None or prior["state"] != row["state"] or prior["status"] != row["status"]
            row["previous_state"] = prior["state"] if prior else None
            row["state_changed"] = changed
            row["state_changed_at"] = now.isoformat() if changed else prior["changed_at"]
            row["conditional_next_steps"] = conditional_pre_move_path(row["bias"], row["trigger"], row["invalidation"], row["targets"])
            self._states[key] = {"state": row["state"], "status": row["status"], "changed_at": row["state_changed_at"]}
            if row["status"] == "AVAILABLE":
                self._observed[row["asset_id"]] = row["observation_timestamp"]
                self._restart.pop(row["asset_id"], None)
        self._last_update = now
        return rows
