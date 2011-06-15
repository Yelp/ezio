#!/usr/bin/python

import testify
from testify.assertions import assert_raises

from tools.tests.test_case import EZIOTestCase

class ValueStore(object):
    pass

display = {'bar': ValueStore()}

class TestCase(EZIOTestCase):

    target_template = 'null_callable'

    def get_display(self):
        return display

    def test(self):
        assert_raises(AttributeError, self.run_templating)

if __name__ == '__main__':
    testify.run()
