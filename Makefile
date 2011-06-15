.PHONY: clean
clean:
	-rm -f ezio/cheetahparser.py
	-find . -name '*.py[co]' -delete
	-find . -name '*.c' -delete
	-find . -name '*.cpp' -delete
	-find . -name '*.so' -delete
