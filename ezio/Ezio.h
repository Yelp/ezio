#include "Python.h"
#include <stdarg.h>

/**
 * Does dotted path lookups, with the path elements being varargs.
 * Attempts dictionary lookup first, fails over to attribute lookup.
 */
static PyObject *resolve_path(PyObject *base, Py_ssize_t path_length, ...) {
    Py_ssize_t counter;
    va_list argslist;

    if (base == NULL) return NULL;
    if (path_length == 0) {
        Py_INCREF(base);
        return base;
    }

    va_start(argslist, path_length);
    for (counter = 0; counter < path_length; counter++) {
        PyObject *name = va_arg(argslist, PyObject *);
        PyObject *temp = PyDict_GetItem(base, name);
        if (temp != NULL) {
            if (counter == path_length - 1) {
                // last path element requires a new reference
                // (since PyDict_GetItem only borrows one)
                Py_INCREF(temp);
            }
            base = temp;
        } else {
            temp = PyObject_GetAttr(base, name);
            if (temp != NULL) {
                if (counter < path_length - 1) {
                    // remove new reference to the intermediate path element
                    // (see caveat about pathological getattr implementations
                    // that could kill their underlying object)
                    Py_DECREF(temp);
                }
                base = temp;
            } else {
                // fail out and return NULL
                base = NULL;
                break;
            }
        }
    }
    va_end(argslist);
    return base;
}
