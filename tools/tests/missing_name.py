#!/usr/bin/python

import testify
from testify.assertions import assert_raises

from tools.tests.test_case import EZIOTestCase

display = {}

class TestCase(EZIOTestCase):

    target_template = 'missing_name'

    def get_display(self):
        return display

    def test(self):
        assert_raises(KeyError, self.run_templating)

if __name__ == '__main__':
    testify.run()
