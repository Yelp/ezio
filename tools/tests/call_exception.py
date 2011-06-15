#!/usr/bin/python

import time

import testify
from testify.assertions import assert_raises

from tools.tests.test_case import EZIOTestCase

class AdHocException(Exception):
    pass

class ExceptionalObject(object):
    def raise_exception(self):
        raise AdHocException('this is an ad-hoc exception')

display = {'bar': ExceptionalObject()}

class TestCase(EZIOTestCase):

    target_template = 'call_exception'

    def get_display(self):
        return display

    def test(self):
        assert_raises(AdHocException, self.run_templating)

if __name__ == '__main__':
    testify.run()
