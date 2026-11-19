import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Response, status

from skopax import checkpresets, promqlchecks
from skopax.messages import msg
from skopax.promqlchecks import BASELINES, COMPARISONS, MODES, SEVERITIES, SOURCES, CustomChecks, Exists, Full, Invalid, Missing, clean

PATH = '/api/admin/custom-checks'


def limits() -> dict:
    return {'checks': promqlchecks.CHECKS_MAX, 'series': promqlchecks.SERIES_MAX, 'cards': promqlchecks.CARDS_MAX, 'querySeconds': promqlchecks.QUERY_TIME_S,
            'for': promqlchecks.FOR_MAX, 'split': promqlchecks.SPLIT_MAX, 'name': promqlchecks.NAME_MAX, 'query': promqlchecks.QUERY_MAX, 'title': promqlchecks.TITLE_MAX,
            'text': promqlchecks.TEXT_MAX, 'periodSeconds': promqlchecks.JOB_PERIOD}


def choices() -> dict:
    return {'source': list(SOURCES), 'op': list(COMPARISONS), 'baselines': list(BASELINES), 'baselineMode': list(MODES), 'severity': list(SEVERITIES)}


def refused(e: Invalid) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(e, Missing) else status.HTTP_409_CONFLICT if isinstance(e, (Exists, Full)) else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, e.text)


def mount(app: FastAPI, checks: CustomChecks, admin: Callable[..., str], activity: Callable[[str, str], None], nudge: Callable[[], None] = lambda: None,
          clock: Callable[[], float] = time.time) -> None:
    app.state.custom_checks = checks
    checkpresets.presets()
    By = Annotated[str, Depends(admin)]
    Given = Annotated[object, Body()]

    @app.get(PATH)
    def get_custom_checks(by: By) -> dict:
        states = checks.defs.states()
        return {'items': [d | {'run': states.get(d['id'])} for d in checks.defs.list()], 'sources': checks.sources(), 'limits': limits(), 'choices': choices(),
                'presets': [{'id': p.id, 'name': p.name, 'nameKey': checkpresets.name_key(p)} for p in checkpresets.presets().values()]}

    @app.post(PATH, status_code=status.HTTP_201_CREATED)
    def post_custom_check(body: Given, by: By) -> dict:
        try:
            made = checks.defs.create(clean(body), by)
        except Invalid as e:
            raise refused(e) from None
        activity(by, msg('custom_checks.added', name=made['name']))
        nudge()
        return made

    @app.post(f'{PATH}/preview')
    def post_custom_check_preview(body: Given, by: By) -> dict:
        try:
            return checks.preview(body, clock())
        except Invalid as e:
            raise refused(e) from None

    def preset_of(preset_id: str) -> checkpresets.Preset:
        found = checkpresets.presets().get(preset_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'no custom check preset with the id {preset_id[:60]}')
        return found

    def values_of(body: object) -> object:
        return body.get('values') if isinstance(body, dict) else body

    @app.get(f'{PATH}/presets')
    def get_custom_check_presets(by: By) -> dict:
        return {'items': [p.view() for p in checkpresets.presets().values()]}

    @app.post(f'{PATH}/presets/{{preset_id}}/render')
    def post_custom_check_preset_render(preset_id: str, body: Given, by: By) -> dict:
        preset = preset_of(preset_id)
        try:
            values, made = preset.fill(values_of(body))
        except Invalid as e:
            raise refused(e) from None
        return {'values': values, 'checks': made}

    @app.post(f'{PATH}/presets/{{preset_id}}', status_code=status.HTTP_201_CREATED)
    def post_custom_check_preset(preset_id: str, body: Given, by: By) -> dict:
        preset = preset_of(preset_id)
        try:
            made = checkpresets.add(checks.defs, preset, values_of(body), by)
        except Invalid as e:
            raise refused(e) from None
        except promqlchecks.Leftover as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, e.text) from None
        activity(by, msg('custom_checks.preset_added', preset=preset.name, name=checkpresets.title_of(preset, made[0]['presetValues']), count=len(made)))
        nudge()
        return {'items': made}

    @app.put(f'{PATH}/{{check_id}}')
    def put_custom_check(check_id: str, body: Given, by: By) -> dict:
        try:
            given = clean({**body, 'id': check_id} if isinstance(body, dict) else body)
            was = checks.defs.get(check_id)
            changed = checks.defs.update(check_id, given, by)
        except Invalid as e:
            raise refused(e) from None
        if was is not None and was.get('enabled') and not changed['enabled']:
            checks.clear(check_id, clock())
        activity(by, msg('custom_checks.changed', name=changed['name']))
        nudge()
        return changed

    @app.delete(f'{PATH}/{{check_id}}', status_code=status.HTTP_204_NO_CONTENT)
    def delete_custom_check(check_id: str, by: By) -> Response:
        gone = checks.defs.delete(check_id)
        if gone is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f'no custom check with the id {check_id[:60]}')
        checks.clear(check_id, clock())
        activity(by, msg('custom_checks.deleted', name=gone['name']))
        nudge()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
