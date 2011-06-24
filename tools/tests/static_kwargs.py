#!/usr/bin/python

import sys

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

display = {'first': '1'}
key = display.keys()[0]
value = display.values()[0]

class TestCase(EZIOTestCase):

    target_template = 'static_kwargs'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, key, value]

    def test(self):
        super(TestCase, self).test()
        assert_equal(self.result.strip(), "Count 1 2 3")

if __name__ == '__main__':
    testify.run()
