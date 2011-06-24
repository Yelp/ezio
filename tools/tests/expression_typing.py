#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_gt

from tools.tests.test_case import EZIOTestCase

def make_typechecker(type_obj):
    def f(val):
        assert isinstance(val, type_obj), '%r is not a %r' % (val, type_obj)
        return "OK"
    return f

display = {
    'is_int': make_typechecker(int),
    'is_long': make_typechecker(long),
    'is_float': make_typechecker(float),
    'is_str': make_typechecker(str),
    'is_unicode': make_typechecker(unicode),
}

class TestCase(EZIOTestCase):

    target_template = 'expression_typing'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()
        # TODO this template contains a utf-8 encoded phrase as bare
        # literal text; tmpl2py converts this into a Python str containing
        # the verbatim bytes. This is not necessarily the wrong behavior,
        # but the result is that self.result ends up being a a str (again,
        # containing the literal bytes), so we have to explicitly call
        # encode() on the phrase before we can find it.
        assert_gt(self.result.find(u'hommage à jack'.encode('utf-8')), 0)

if __name__ == '__main__':
    testify.run()
