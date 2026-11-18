from tests.plugins.base import minimal

PLUGIN = minimal('order-a', 'Order A', after=('order-c',))
