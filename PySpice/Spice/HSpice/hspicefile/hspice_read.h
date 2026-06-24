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
#include "Python.h"

// Structure for fast vector access
struct FastArray
{
	char *data;
	char *pos;
	Py_ssize_t stride;
	Py_ssize_t length;
};

// Python callable function
static PyObject *HSpiceRead(PyObject *self, PyObject *args);

#ifdef LINUX
#define __declspec(a) extern
#endif
