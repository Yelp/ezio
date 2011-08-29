#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

def first():
    return "first"

def second():
    return "unreachable"

def third():
    return "unreachable"

display = { 'first': first, 'second': second, 'third': third }

class TestCase(EZIOTestCase):

    target_template = 'compatibility'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()
        assert_equal(self.lines, ['first', 'third'])

if __name__ == '__main__':
    testify.run()
