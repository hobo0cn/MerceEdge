from merceedge.core import WireLoad

class FilterWireLoad(WireLoad):
    name = 'filter_wireload'
    
    def __init__(self, init_params={}):
        super(FilterWireLoad, self).__init__(init_params)
    
    def process(self, input_data):
        # TODO
        pass