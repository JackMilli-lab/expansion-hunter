"""Authenticated manual arithmetic; no orders, exchange access or trade storage."""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from .intelligence_api import local_or_authenticated
from .trade_calculator import (
    CalculatorRequest,
    CalculatorResult,
    PortfolioInput,
    PortfolioResult,
    calculate,
    calculate_portfolio,
)


def _safe_error(value: Any) -> Any:
    # Python's JSON decoder accepts NaN/Infinity. Validation rejects them; its
    # response must itself remain valid JSON, rather than raising a server 500.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _safe_error(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_safe_error(item) for item in value]
    if isinstance(value, Exception):
        return str(value)
    return value


class CalculatorRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def validated(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": _safe_error(exc.errors())})

        return validated


def install_routes(app, authorize: Callable):
    router = APIRouter(
        prefix="/api/war-room",
        route_class=CalculatorRoute,
        dependencies=[Depends(authorize), Depends(local_or_authenticated)],
        tags=["Manual decision support"],
    )

    @router.post("/calculate", response_model=CalculatorResult)
    async def calculate_request(payload: CalculatorRequest):
        try:
            return calculate(payload)
        except (ValidationError, OverflowError, ZeroDivisionError) as exc:
            raise HTTPException(422, "Inputs produce a non-finite or undefined calculation") from exc

    @router.post("/portfolio", response_model=PortfolioResult)
    async def calculate_positions(payload: PortfolioInput):
        try:
            return calculate_portfolio(payload)
        except (ValidationError, OverflowError, ZeroDivisionError) as exc:
            raise HTTPException(422, "Inputs produce a non-finite or undefined portfolio calculation") from exc

    # These pure routes have no startup/shutdown handlers. Register their
    # already-prefixed APIRoute objects directly to preserve the existing
    # inspectable app.routes interface across FastAPI deferred-router versions.
    app.router.routes.extend(router.routes)
