from skopax.contract import Check, PassResult, Plugin, Sweep


def run(ctx) -> Sweep:
    return Sweep(PassResult('base', True, 'ok', [], {}, ()))


def minimal(id: str, name: str, **over) -> Plugin:
    values = dict(id=id, name=name, version='0.1.0', category='other', description=f'{name} fixture', icon='demo-icon.svg', author='Skopax contributors', licence='Apache-2.0',
                  core='>=0.1,<1', checks=(Check(id, name, 900, 0, (id,), run),))
    return Plugin(**(values | over))
