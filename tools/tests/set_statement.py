#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

def add_pipes(my_string):
    return "|%s|" % (my_string,)

display = {
    'add_pipes': add_pipes,
    'bar': 'bar',
    'bat': 'bat',
    'quux': 'quux',
}

class TestCase(EZIOTestCase):

    target_template = 'set_statement'

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues()) + [os, os.__file__]

    def test(self):
        super(TestCase, self).test()

        assert_in('my_func: bar', self.result)
        assert_in('respond: quux', self.result)
        assert_in('os.__file__: %s' % (os.__file__,), self.result)
        assert_in('again: %s' % (os.__file__,), self.result)
        assert_in('respond_reassignment: bat', self.result)
        assert_in('in_pipes: |bat|', self.result)

if __name__ == '__main__':
    testify.run()
