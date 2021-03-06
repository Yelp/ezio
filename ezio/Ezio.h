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

/** Helper for tuple unpacking; copy the internal buffer of a list or tuple
  into a destination array, checking the size against `len`, and setting
  appropriate exceptions on failure. Returns 0 on failure and 1 on success.
  */
static int sequence_copy(PyObject *seq, Py_ssize_t len, PyObject **dest) {
    if (PyList_Check(seq)) {
        if (PyList_GET_SIZE(seq) != len) {
            PyErr_SetString(PyExc_ValueError, "Invalid sequence size");
            return 0;
        }
        // copy the internal buffer
        memcpy(dest, ((PyListObject *) seq)->ob_item, sizeof(PyObject *) * len);
        return 1;
    } else if (PyTuple_Check(seq)) {
        if (PyTuple_GET_SIZE(seq) != len) {
            PyErr_SetString(PyExc_ValueError, "Invalid sequence size");
            return 0;
        }
        memcpy(dest, ((PyTupleObject *) seq)->ob_item, sizeof(PyObject *) * len);
        return 1;
    }

    PyErr_SetString(PyExc_TypeError, "Cannot unpack non-list/tuple.");
    return 0;
}

namespace ezio_templates {

    /** Base C++ class for all templates. */
    class ezio_base_template {
        public:
            // display dictionary: namespace for dynamic template lookups
            PyObject *display;
            // a python list containing the pieces of the document being assembled
            PyObject *transaction;
            // if not NULL, a Python object that can be the target of dynamic references to `self`
            PyObject *self_ptr;

            ezio_base_template(PyObject *display, PyObject *transaction, PyObject *self_ptr) :
                display(display), transaction(transaction), self_ptr(self_ptr) {}
    };
}

/* Status codes that can be returned by the coercion/filtering code. */
static const int COERCED_TO_STR = 0;
static const int COERCED_TO_UNICODE = 1;
static const int COERCE_FAILED = 2;

/** A callback type; a filter must take in a PyObject * and return a new reference
  Implement this interface to transform the elements of the templating transaction
  at "join time".
  */
typedef PyObject* (*Ezio_Filter)(PyObject *operand, void *closure_data);

/** Ezio_Filter that transforms objects into Unicodes. */
PyObject *default_unicode_filter(PyObject *item, void *closure_data) {
    if (PyUnicode_Check(item)) {
        Py_INCREF(item);
        return item;
    }

    // promote a common case in PyObject_Unicode, the decoding of strings:
    if (PyString_Check(item)) {
        return PyUnicode_Decode(PyString_AS_STRING(item),
                PyString_GET_SIZE(item), NULL, NULL);
    } else {
        // this is the `unicode` built-in:
        return PyObject_Unicode(item);
    }
}

/** Apply an Ezio_Filter that returns unicodes to a list `transaction`;
  return the total length of the unicodes (for buffer pre-allocation),
  and modify `status` to reflect the success or failure of the coercions.

  This implementation (and others here) is unsafe in general because
  it re-enters the interpreter without re-checking list bounds.
  This is OK in this case because only internal C++ code has a reference
  to `transaction`, so the list bounds cannot vary.

  In the future, this is the place where we'll implement HTML escaping,
  by passing an Ezio_Filter that does escaping intelligently.
  */
Py_ssize_t apply_unicode_filter(PyObject *transaction, int *status,
                                Ezio_Filter filter, void *closure_data) {
    if (!(transaction && PyList_CheckExact(transaction))) {
        *status = COERCE_FAILED;
        return 0;
    }

    Py_ssize_t size = PyList_GET_SIZE(transaction);
    Py_ssize_t seqlen = 0;
    Py_ssize_t i;
    for (i = 0; i < size; i++) {
        PyObject *item = PyList_GET_ITEM(transaction, i);
        PyObject *filtered_item = filter(item, closure_data);
        if (filtered_item == NULL) {
            *status = COERCE_FAILED;
            return 0;
        }
        // replace the list entry with the filtered_item:
        Py_DECREF(item);
        PyList_SET_ITEM(transaction, i, filtered_item);
        seqlen += PyUnicode_GET_SIZE(filtered_item);
    }

    *status = COERCED_TO_UNICODE;
    return seqlen;
}


/** Attempt to coerce all elements of `transaction` to string,
  unless one of them is a unicode, in which case coerce everything
  to unicode. This is more or less what standard str.join() does
  (except, of course, that it performs coercion and modifies `transaction`
  in place with the results of the coercions).
  */
Py_ssize_t coerce_all(PyObject *transaction, int *status) {
    if (!(transaction && PyList_CheckExact(transaction))) {
        *status = COERCE_FAILED;
        return 0;
    }

    Py_ssize_t size = PyList_GET_SIZE(transaction);
    Py_ssize_t seqlen = 0;
    Py_ssize_t i;
    for (i = 0; i < size; i++) {
        PyObject *item = PyList_GET_ITEM(transaction, i);
        if (!PyString_Check(item)) {
            if (PyUnicode_Check(item)) {
                // coerce all transaction elements to unicode using the default unicode filter
                return apply_unicode_filter(transaction, status, default_unicode_filter, NULL);
            } else {
                PyObject *coerced_item = PyObject_Str(item);
                if (coerced_item != NULL) {
                    // discard the ref to the old value, steal one to the new one
                    Py_DECREF(item);
                    PyList_SET_ITEM(transaction, i, coerced_item);
                    item = coerced_item;
                } else {
                    *status = COERCE_FAILED;
                    return 0;
                }
            }
        }
        seqlen += PyString_GET_SIZE(item);
    }

    *status = COERCED_TO_STR;
    return seqlen;
}

/** Assuming `transaction` contains only strings and their total length is
  `total_length`, concatenate them all and return a new reference to the
  resulting string.
  */
PyObject *concatenate_strings(PyObject *transaction, Py_ssize_t total_length) {
    PyObject *res = PyString_FromStringAndSize(NULL, total_length);
    if (res == NULL) {
        return NULL;
    }

    char *buf = PyString_AS_STRING(res);
    Py_ssize_t size = PyList_GET_SIZE(transaction);
    Py_ssize_t i;
    for (i = 0; i < size; i++) {
        PyObject *item = PyList_GET_ITEM(transaction, i);
        size_t n = PyString_GET_SIZE(item);
        Py_MEMCPY(buf, PyString_AS_STRING(item), n);
        buf += n;
    }

    return res;
}

/** Like concatenate_strings, but for unicodes. Mostly copied and pasted from the above.
  */
PyObject *concatenate_unicodes(PyObject *transaction, Py_ssize_t total_length) {
    PyObject *res = PyUnicode_FromUnicode(NULL, total_length);
    if (res == NULL) {
        return NULL;
    }

    Py_UNICODE *buf = PyUnicode_AS_UNICODE(res);
    Py_ssize_t size = PyList_GET_SIZE(transaction);
    Py_ssize_t i;
    for (i = 0; i < size; i++) {
        PyObject *item = PyList_GET_ITEM(transaction, i);
        Py_ssize_t n = PyUnicode_GET_SIZE(item);
        Py_UNICODE_COPY(buf, PyUnicode_AS_UNICODE(item), n);
        buf += n;
    }

    return res;
}

/** Combines coerce_all, concatenate_strings, and concatenate_unicodes
  to make an analogue of str.join() that coerces non-strings to strings
  (and, like str.join(), coerces everything to unicode if unicode is encountered).
  */
PyObject *ezio_concatenate(PyObject *transaction) {
    if (!(transaction && PyList_CheckExact(transaction))) {
        return NULL;
    }

    int status;
    Py_ssize_t total_length = coerce_all(transaction, &status);
    if (status == COERCE_FAILED) {
        // propagates exceptions raised during coercion:
        return NULL;
    }

    if (status == COERCED_TO_STR) {
        return concatenate_strings(transaction, total_length);
    } else if (status == COERCED_TO_UNICODE) {
        return concatenate_unicodes(transaction, total_length);
    } else {
        // internal error
        PyErr_SetString(PyExc_SystemError, "Invalid coercion status.");
        return NULL;
    }
}

/**
  This is equivalent to PyObject_GetItem, but it promotes a common case.
  Copied and pasted from ceval.c's (i.e., the interpreter's) handling of the
  BINARY_SUBSCR opcode.
  */
PyObject *optimized_getitem(PyObject *expr, PyObject *subscript) {
    PyObject *x;
    if (PyList_CheckExact(expr) && PyInt_CheckExact(subscript)) {
        /* INLINE: list[int] */
        Py_ssize_t i = PyInt_AsSsize_t(subscript);
        if (i < 0)
            i += PyList_GET_SIZE(expr);
        if (i >= 0 && i < PyList_GET_SIZE(expr)) {
            x = PyList_GET_ITEM(expr, i);
            Py_INCREF(x);
        }
        else
            goto slow_get;
    }
    else
        slow_get:
            x = PyObject_GetItem(expr, subscript);
    return x;
}

/**
  Implement the unary `not` operation as a C-API call returning a borrowed reference.
  We could explicitly declare this inline if we wanted.
  */
PyObject *unary_not(PyObject *expr) {
    int result = PyObject_IsTrue(expr);
    if (result == 0) return Py_True;
    else if (result == 1) return Py_False;
    // the error condition is -1
    return NULL;
}
