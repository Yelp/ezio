#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

seq = ['a', 'b', 'c']

display = {
        'seq': seq,
}

class TestCase(EZIOTestCase):

    target_template = 'builtins'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [seq]

    def test(self):
        super(TestCase, self).test()

        expected_lines = [
                ''.join(' %d' % (num,) for num in xrange(10)),
                '0 a',
                '1 b',
                '2 c',
        ]

        assert_equal(self.lines, expected_lines)

if __name__ == '__main__':
    testify.run()
