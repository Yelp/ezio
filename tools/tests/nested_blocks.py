#!/usr/bin/python

import testify
from testify.assertions import assert_not_in, assert_lt

from tools.tests.test_case import EZIOTestCase

display = {'asdf': 'asdf', 'bsdf': 'bsdf'}

class TestCase(EZIOTestCase):

    target_template = 'nested_blocks'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()

        before_the_block_index = self.result.find('before_the_block')
        outer_block_index = self.result.find('outer_block')
        asdf_index = self.result.find('asdf')
        inner_block_index = self.result.find('inner_block')
        bsdf_index = self.result.find('bsdf')

        indices = (before_the_block_index, outer_block_index, asdf_index, inner_block_index, bsdf_index)
        assert_not_in(-1, indices)

        for i in xrange(1, len(indices)):
            assert_lt(indices[i-1], indices[i])

if __name__ == '__main__':
    testify.run()
