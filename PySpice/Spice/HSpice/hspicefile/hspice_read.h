/*
 * Copyright (c) 2026 Daniel Schmeer
 * Copyright (c) 2009 Arpad Buermen (PyOPUS Project)
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef HSPICE_READ_H
#define HSPICE_READ_H

#include "Python.h"
#include <stdbool.h>

// Structure for fast vector access
struct FastArray {
  char *data;
  char *pos;
  Py_ssize_t stride;
  Py_ssize_t length;
};

typedef enum {
  HSPICE_FORMAT_UNKNOWN,
  HSPICE_FORMAT_9007,
  HSPICE_FORMAT_9601,
  HSPICE_FORMAT_2001,
  HSPICE_FORMAT_2013
} HSpiceFormat;

struct ParserContext {
  HSpiceFormat format;
  bool swap;
  FILE *f;
  const char *fileName;
  int debugMode;
};

// Python callable function
static PyObject *HSpiceRead(PyObject *self, PyObject *args);

// Python exceptions
static PyObject *HSpiceParseError;

#ifdef LINUX
#define __declspec(a) extern
#endif

#endif // HSPICE_READ_H
