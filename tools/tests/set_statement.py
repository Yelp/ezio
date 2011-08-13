#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

def add_pipes(my_string):
    return "|%s|" % (my_string,)

DEFAULT_ARGUMENT = 'this is the first call to my_func'
def get_default_str(): return DEFAULT_ARGUMENT

display = {
    'add_pipes': add_pipes,
    'bar': 'bar',
    'bat': 'bat',
    'quux': 'quux',
    'get_default_str': get_default_str,
}

class TestCase(EZIOTestCase):

    target_template = 'set_statement'

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues()) + [os, os.__file__, DEFAULT_ARGUMENT, get_default_str]

    def test(self):
        super(TestCase, self).test()

        expected_lines = [
                'respond: quux',
                'my_func: bar',
                'os.__file__: %s' % (os.__file__,),
                'again: %s' % (os.__file__,),
                'respond_reassignment: bat',
                'in_pipes: |bat|',
                DEFAULT_ARGUMENT,
                'this is the second call to my_func',
        ]

if __name__ == '__main__':
    testify.run()
