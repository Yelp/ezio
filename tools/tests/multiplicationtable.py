#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

class Multiplier(object):
    def multiply(self, first, second):
        return str(first * second)

bag = range(100)

display = {'bar': Multiplier(), 'bag': bag}

class TestCase(EZIOTestCase):

    target_template = 'multiplicationtable'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()

        # 69 * 69
        assert '<td>4761</td>' in self.result

if __name__ == '__main__':
    testify.run()
