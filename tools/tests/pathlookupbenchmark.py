#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

bag = range(100)

bar = {
    'bat': '1',
    'baz': {'bat': '2'},
    'bam': {'baz': {'bat': '3'}},
    'bal': {'bam': {'baz': {'bat': '4'}}}
}

display = {'bar': bar, 'bag': bag}

class TestCase(EZIOTestCase):

    target_template = 'pathlookupbenchmark'

    def get_display(self):
        return display

if __name__ == '__main__':
    testify.run()
