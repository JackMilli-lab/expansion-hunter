"""Authenticated manual paper journal API; no exchange execution endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from .calculator_api import CalculatorRoute
from .intelligence_api import local_or_authenticated
from .models import utcnow
from .paper_journal import PaperCloseRequest, PaperJournal, PaperOpenRequest, PaperPosition


def install_routes(app, authorize: Callable):
    router = APIRouter(prefix="/api/war-room/journal", route_class=CalculatorRoute,
        dependencies=[Depends(authorize)], tags=["Manual paper journal"])

    async def journal(request: Request) -> PaperJournal:
        store = getattr(request.app.state, "store", None)
        if store is None:
            raise HTTPException(503, "Operational database is not available")
        return await asyncio.to_thread(PaperJournal, store)

    async def call(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, "Paper position not found") from exc
        except ValidationError as exc:
            raise HTTPException(422, "Inputs produce a non-finite or invalid paper calculation") from exc
        except (OverflowError, ZeroDivisionError) as exc:
            raise HTTPException(422, "Inputs produce a non-finite or undefined paper calculation") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("")
    async def list_positions(request: Request,
        status: Literal["OPEN", "CENSORED", "AMBIGUOUS", "CLOSED"] | None = None,
        limit: int = Query(default=100, ge=1, le=1000)):
        positions = await call((await journal(request)).list, status=status, limit=limit)
        return {"mode": "PAPER_ONLY", "positions": positions, "live_orders": False}

    @router.post("", response_model=PaperPosition,
                 dependencies=[Depends(local_or_authenticated)])
    async def open_position(request: Request, payload: PaperOpenRequest):
        return await call((await journal(request)).open, payload, at=utcnow())

    @router.get("/analytics")
    async def analytics(request: Request, initial_balance: float | None = Query(default=None, gt=0),
                        quote_currency: str | None = Query(default=None, min_length=2, max_length=16)):
        return await call((await journal(request)).analytics, initial_balance=initial_balance, quote_currency=quote_currency)

    @router.get("/portfolio")
    async def portfolio(request: Request, quote_currency: str | None = Query(default=None, min_length=2, max_length=16)):
        return await call((await journal(request)).portfolio, quote_currency=quote_currency)

    @router.get("/{position_id}", response_model=PaperPosition)
    async def get_position(request: Request, position_id: str):
        result = await call((await journal(request)).get, position_id)
        if result is None:
            raise HTTPException(404, "Paper position not found")
        return result

    @router.get("/{position_id}/history")
    async def history(request: Request, position_id: str, as_of: datetime | None = None):
        if as_of is not None and as_of.tzinfo is None:
            raise HTTPException(422, "as_of must include a timezone")
        service = await journal(request)
        if await call(service.get, position_id) is None:
            raise HTTPException(404, "Paper position not found")
        return {"mode": "PAPER_ONLY", "history": await call(service.history, position_id, as_of=as_of)}

    @router.post("/{position_id}/close", response_model=PaperPosition,
                 dependencies=[Depends(local_or_authenticated)])
    async def close_position(request: Request, position_id: str, payload: PaperCloseRequest):
        return await call((await journal(request)).close, position_id, payload, at=utcnow())

    # Match existing calculator registration: no deferred include_router object
    # that disappears from the legacy inspectable app.routes interface.
    app.router.routes.extend(router.routes)
