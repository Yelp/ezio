#!/usr/bin/python

import testify
from testify.assertions import assert_not_equal, assert_lt

from tools.tests.test_case import EZIOTestCase

display = {'asdf': 'asdf'}

class TestCase(EZIOTestCase):

    target_template = 'blocks'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()

        before_the_block_index = self.result.find('before_the_block')
        in_the_block_index = self.result.find('in_the_block')
        asdf_index = self.result.find('asdf')

        assert_not_equal(before_the_block_index, -1)
        assert_not_equal(in_the_block_index, -1)
        assert_not_equal(asdf_index, -1)

        assert_lt(before_the_block_index, in_the_block_index)
        assert_lt(in_the_block_index, asdf_index)

if __name__ == '__main__':
    testify.run()
