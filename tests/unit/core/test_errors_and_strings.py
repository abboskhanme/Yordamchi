"""core/errors ↔ core/strings: every code has its Uzbek message (`test_error_catalog`, CONVENTIONS §7, §11)."""

import inspect
from collections.abc import Callable
from typing import cast

import pytest

from yordamchi.core import errors
from yordamchi.core import strings as S
from yordamchi.core.text import placeholders, render


def _concrete_errors() -> list[type[errors.YordamchiError]]:
    found: list[type[errors.YordamchiError]] = []
    for _, value in inspect.getmembers(errors, inspect.isclass):
        if issubclass(value, errors.YordamchiError) and value not in {
            errors.YordamchiError,
            errors.DomainError,
            errors.ExternalError,
        }:
            found.append(value)
    return found


def _sample(error_type: type[errors.YordamchiError]) -> errors.YordamchiError:
    params = inspect.signature(error_type.__init__).parameters
    kwargs: dict[str, str | int] = {}
    for name, parameter in params.items():
        if name == "self" or parameter.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = 1 if parameter.annotation in {"int", int} else "x"
    # Each concrete class fixes its own code and takes keyword params only.
    build = cast(Callable[..., errors.YordamchiError], error_type)
    return build(**kwargs)


def test_error_catalog_covers_every_code_and_nothing_else() -> None:
    assert set(S.ERRORS) == set(errors.ErrorCode)
    stray = [
        name
        for name in vars(S)
        if name.startswith("ERR_") and name.removeprefix("ERR_") not in errors.ErrorCode.__members__
    ]
    assert stray == []


@pytest.mark.parametrize("error_type", _concrete_errors(), ids=lambda t: t.__name__)
def test_every_error_renders_its_owner_message(error_type: type[errors.YordamchiError]) -> None:
    error = _sample(error_type)
    text = S.ERRORS[error.code](**error.params)
    assert text
    assert str(error) == error.code.value  # params never reach a log line through str(error)


def test_external_errors_say_whether_they_are_retryable() -> None:
    assert errors.RateLimited(provider="google", retry_after=2.0).retryable
    assert errors.CallTimedOut(operation="stt").retryable
    assert not errors.QuotaExhausted(provider="openrouter").retryable
    assert not errors.AuthFailed(provider="google").retryable


@pytest.mark.parametrize("name", sorted(name for name, value in vars(S).items() if isinstance(value, S.Msg)), ids=str)
def test_every_catalog_entry_renders(name: str) -> None:
    entry: S.Msg = getattr(S, name)
    assert entry(**dict.fromkeys(entry.params, "X"))


def test_a_catalog_entry_refuses_missing_or_unused_params() -> None:
    with pytest.raises(ValueError, match="missing"):
        S.HEALTH_ONLINE()
    with pytest.raises(ValueError, match="unused"):
        S.HEALTH_ONLINE(time="09:00", extra="x")
    assert S.HEALTH_ONLINE(time="09:00") == "Onlayn: 09:00"
    assert S.WAKE_GAP(from_="01:10", to="07:45", n=3) == "Mac uxlagan edi 01:10–07:45, 3 ta xabar qayta ishlanmoqda"


def test_placeholders_must_be_plain_names() -> None:
    with pytest.raises(ValueError, match="plain name"):
        placeholders("{0} and {a.b}")
    assert render("{a}-{b}", {"a": 1, "b": "x"}) == "1-x"
