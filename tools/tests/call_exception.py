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

an_exceptional_object = ExceptionalObject()

display = {'bar': an_exceptional_object}

class TestCase(EZIOTestCase):

    target_template = 'call_exception'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [an_exceptional_object]

    def test(self):
        self.perform_exception_test(AdHocException)

if __name__ == '__main__':
    testify.run()
