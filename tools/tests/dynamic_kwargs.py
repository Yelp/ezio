#!/usr/bin/python

import sys

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

def my_callable(a, b, c, d=None, e=None):
    return "a %s b %s c %s d %s e %s" % (a, b, c, d, e)

def my_callable_no_posargs(x=None, y=None):
    return "x %s y %s" % (x, y)

def my_callable_for_newref_testing(new_ref, borrowed_ref=None):
    return "new_ref %s borrowed_ref %s" % (new_ref, borrowed_ref)

NEW_REF_STR = 'new_ref'
def get_new_ref_str():
    return NEW_REF_STR

display = {
    'my_callable': my_callable,
    'my_callable_no_posargs': my_callable_no_posargs,
    'get_new_ref_str': get_new_ref_str,
    'my_callable_for_newref_testing': my_callable_for_newref_testing,
}

class TestCase(EZIOTestCase):

    target_template = 'dynamic_kwargs'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, my_callable, my_callable_no_posargs, NEW_REF_STR]

    def test(self):
        super(TestCase, self).test()
        assert_in('a a b b c c d d e e', self.result)
        assert_in('x x y y', self.result)
        assert_in('new_ref new_ref borrowed_ref borrowed_ref', self.result)

if __name__ == '__main__':
    testify.run()
