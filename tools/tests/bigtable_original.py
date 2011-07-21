"""
This is the original benchmark from Spitfire, i.e., with numbers as keys
that must be converted to string at template time.
"""

import testify

from tools.tests.test_case import EZIOTestCase

table = [dict(a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,j=10)
          for x in range(1000)]

display = {'table': table}

class TestCase(EZIOTestCase):
    target_template = 'bigtable'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, display['table'], display.keys()[0], display.values()[0]]

if __name__ == '__main__':
    testify.run()
