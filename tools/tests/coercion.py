#!/usr/bin/python
# -*- coding: utf-8 -*-

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

class MyStringable(object):
    def __str__(self):
        return 'ohai'

display = {
        'first': 1,
        'second': 2.0,
        'third': 'asdf',
        'fourth': MyStringable(),
        # this is a UTF-8 encoding of a unicode string, but here Python interprets it
        # as a byte sequence (i.e., an instance of the 'str' class), since it's a literal
        # without a preceding u
        'fifth': 'hommage à jack',
}

class SimpleTestCase(EZIOTestCase):

    target_template = 'coercion'

    expected_result_type = str

    def get_display(self):
        return display

    def get_refcountables(self):
        return display.values()

    def test(self):
        super(SimpleTestCase, self).test()
        assert_equal(
            self.result.strip().split('\n'),
            ['first', '1', 'second', '2.0', 'third', 'asdf', 'fourth', 'ohai',
             'fifth', 'hommage à jack'
            ]
        )

if __name__ == '__main__':
    testify.run()
