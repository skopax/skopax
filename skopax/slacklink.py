PLUGIN_ID = 'slack'


class SlackLink:
    def __init__(self, runtime):
        self.runtime = runtime

    def _secret(self, key: str) -> str:
        primary = self.runtime.instances.primary(PLUGIN_ID)
        if primary is None:
            return ''
        serving = next((i for i in self.runtime.instances_of(PLUGIN_ID) if i.name == primary.name), None)
        return (serving.secrets.get(key) or '').strip() if serving is not None else ''

    def token(self) -> str:
        return self._secret('bot-token')

    def app_token(self) -> str:
        return self._secret('app-token')
