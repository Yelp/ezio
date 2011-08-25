#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

fourteen = "fourteen"
def get_fourteen():
    return fourteen

true_obj = [1, 2, 3]
false_obj = set()
oks = ['OK']
nos = ['NO']

mykey = (1, 2, 3, 4, 5)
mylist = ['OK', 'OK']
mydict = { 'a': 'OK', mykey: 'OK' }

display = {
        'get_fourteen': get_fourteen,
        'true_obj': true_obj,
        'false_obj': false_obj,
        'ok': 'OK',
        'no': 'NO',
        'oks': oks,
        'nos': nos,
        'mykey': mykey,
        'mydict': mydict,
        'mylist': mylist,
        'myindex': 1,
        'one': 1,
}

class TestCase(EZIOTestCase):

    target_template = 'misc_features'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [fourteen, true_obj, false_obj, oks, nos] + oks + nos + [mykey, mydict, mylist]

    def test(self):
        super(TestCase, self).test()
        expected_result = ([str(n) for n in xrange(1, 11)] +
                ['11', 'twelve', '13', 'fourteen', '15'] + ['OK'] * 12)
        assert_equal(self.lines, expected_result)

if __name__ == '__main__':
    testify.run()
