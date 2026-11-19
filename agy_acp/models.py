"""Real ACP model catalog parsing, model switching, and thought-level selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import AgyError

if TYPE_CHECKING:
    from .transport import AcpClient


def options(result: dict) -> list[dict]:
    return [opt for opt in (result.get("configOptions") or []) if isinstance(opt, dict)]


def find_option(config: list[dict], category: str) -> dict | None:
    return next(
        (
            opt
            for opt in config
            if opt.get("id") == category or opt.get("category") == category
        ),
        None,
    )


def choices(option: dict | None) -> dict[str, str]:
    found: dict[str, str] = {}
    if not option:
        return found
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("value"), str):
            found[item["value"]] = str(item.get("name", item["value"]))
        else:
            found.update(choices(item))
    return found


def catalog_names(result: dict) -> dict[str, str]:
    found = choices(find_option(options(result), "model"))
    model_data = result.get("models") or {}
    for model in model_data.get("availableModels", []):
        if isinstance(model, dict) and isinstance(model.get("modelId"), str):
            found[model["modelId"]] = str(model.get("name", model["modelId"]))
    if not found:
        raise AgyError(
            "NO_MODELS",
            "当前 ACP 目录没有可用模型，请检查 agy CLI 登录与账号模型权限。",
        )
    return found


def catalog(result: dict) -> list[str]:
    return list(catalog_names(result))


def _matches_model(
    config: list[dict], requested: str, *, allow_unreported: bool = False
) -> tuple[bool, bool]:
    actual = find_option(config, "model")
    current_model = actual.get("currentValue") if actual else None
    if current_model is None:
        return (allow_unreported, False)
    if current_model == requested:
        return (True, False)
    level = find_option(config, "thought_level")
    current_level = level.get("currentValue") if level else None
    if (
        isinstance(current_model, str)
        and isinstance(current_level, str)
        and f"{current_model}-{current_level}" == requested
    ):
        return (True, True)
    return (False, False)


async def select(client: AcpClient, result: dict, model: str, effort: str) -> list[dict]:
    available = catalog(result)
    if model not in available:
        raise AgyError("MODEL", f"所选模型 {model} 不在当前可用目录中，请先同步模型列表。")
    config = options(result)
    model_option = find_option(config, "model")
    matched, has_embedded_level = _matches_model(config, model, allow_unreported=False)
    if not matched and not model_option:
        matched = (result.get("models") or {}).get("currentModelId") == model
    if not matched:
        if model_option:
            changed = await client.set_option(model_option["id"], model)
            config = options(changed) or config
            matched, has_embedded_level = _matches_model(
                config, model, allow_unreported=True
            )
            if not matched:
                raise AgyError("MODEL", "AGY 未切换到请求的模型。")
        elif (result.get("models") or {}).get("availableModels"):
            changed = await client.request(
                "session/set_model",
                {"sessionId": client.session_id, "modelId": model},
                client.settings.startup_timeout,
            )
            config = options(changed)
        else:
            raise AgyError("UNSUPPORTED", "此 CLI 没有提供模型选择能力。")
    if effort == "default" or has_embedded_level:
        return config
    level = find_option(config, "thought_level")
    allowed = choices(level)
    if not level or effort not in allowed:
        raise AgyError(
            "UNSUPPORTED",
            "当前模型不支持所选思考档位，请改为 default 或 CLI 实际支持的档位。",
        )
    if level.get("currentValue") != effort:
        changed = await client.set_option(level["id"], effort)
        actual = find_option(options(changed), "thought_level")
        if actual and actual.get("currentValue") not in {None, effort}:
            raise AgyError("UNSUPPORTED", "AGY 未应用所选思考档位。")
        config = options(changed) or config
    return config
