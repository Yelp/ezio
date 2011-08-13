#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Tests for the #call statement.
"""

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

def add_tags(my_string, tag='div'):
    return "<%s>\n%s\n</%s>" % (tag, my_string, tag,)

display = {
    'add_tags': add_tags,
    'city': 'baltimore',
    'destination': "king's landing",
}

class TestCase(EZIOTestCase):

    target_template = 'call_statement'

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues()),

    def test(self):
        super(TestCase, self).test()

        assert_equal(self.lines,
                ['<div>', 'hi from baltimore', '</div>',
                 '<p>', "to king's landing!", '</p>'])

if __name__ == '__main__':
    testify.run()
