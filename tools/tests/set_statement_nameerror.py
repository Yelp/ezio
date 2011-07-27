#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

BAR_VAL = 'thisisthereturnvalueofthebarfunction'
def bar():
    return BAR_VAL

display = {
    'bar': bar,
}

class TestCase(EZIOTestCase):

    target_template = 'set_statement_nameerror'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [bar, BAR_VAL]

    def test(self):
        self.perform_exception_test(NameError)
        assert_equal(self.exception.args[0], 'local_var_2')

if __name__ == '__main__':
    testify.run()
