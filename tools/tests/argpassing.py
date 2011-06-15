#!/usr/bin/python

import sys

import testify

from tools.tests.test_case import EZIOTestCase

SHIBBOLETH = 'shibboleth'

display = {'first': 'shibboleth'}
key = display.keys()[0]
value = display.values()[0]

class TestCase(EZIOTestCase):

    target_template = 'argpassing'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, key, value]

    def test(self):
        super(TestCase, self).test()
        assert SHIBBOLETH in self.result

if __name__ == '__main__':
    testify.run()
