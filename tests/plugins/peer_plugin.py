from skopax.contract import Check, Field, PassResult, Plugin, Sweep, anomaly


def echo(ctx) -> Sweep:
    what = ctx.peer('source').info.config['url']
    card = anomaly('peer', 'peer', 'shop', 'checkout-service', ctx.instance.name, 'warn', 1, None, None, '', 'Peer echo', what)
    return Sweep(PassResult('echo', True, 'ok', [card], {}, ('peer',)))


PLUGIN = Plugin(
    id='peer', name='Peer', version='0.1.0', category='other', description='Reads through an instance of the demo plug-in.', icon='demo-icon.svg', author='Skopax contributors', licence='Apache-2.0',
    core='>=0.1,<1', uses=('demo',), fields=(Field('source', 'Source', 'instance', required=True, choices=('demo',)),),
    checks=(Check('echo', 'Peer echo', 900, 0, ('peer',), echo),),
)
