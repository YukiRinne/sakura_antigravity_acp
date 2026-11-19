"""Google Antigravity ACP 模型提供方插件入口。"""

from __future__ import annotations

from sakura_model_provider import ModelProvider

if __package__:
    from .profiles import AntigravityProfiles, SERVICE_KEY
else:
    from profiles import AntigravityProfiles, SERVICE_KEY


class AntigravityPlugin(ModelProvider):
    def setup(self, context) -> None:
        profiles = AntigravityProfiles(context)
        super().setup(
            context,
            profiles=profiles,
            service_key=SERVICE_KEY,
            label="Google Antigravity",
        )

    def execute(self, settings, request, **kwargs):
        return self.profiles.execute(settings, request, **kwargs)

    def diagnostic_secrets(self, settings):
        proxy = settings.get("proxy_url", "") if isinstance(settings, dict) else ""
        return (proxy,) if proxy else ()

    def scope_closed(self, payload) -> None:
        super().scope_closed(payload)
        self.profiles.close_scope(payload["pluginId"], payload["scopeId"])
