/*
 * Copyright (c) 2026 Daniel Schmeer
 * Copyright (c) 2009 Janez Puhan (PyOPUS Project)
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

// (c) Janez Puhan
// Date: 18.5.2009
// HSpice binary file import module
// Modifications:
// 1. All vector names are converted to lowercase.
// 2. In vector names 'v(*' is converted to '*'.
// 3. No longer try to close a file after failed fopen (caused a crash).
// Author: Arpad Buermen

// Note that in Windows we do not use Debug compile because we don't have the
// debug version of Python libraries and interpreter. We use Release version
// instead where optimizations are disabled. Such a Release version can be
// debugged.

#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include "hspice_read.h"
#include "Python.h"
#include "numpy/arrayobject.h"
#include "numpy/npy_math.h"
#include <errno.h>
#include <limits.h>

// Methods table
static PyMethodDef module_methods[] = {
    {"hspice_read", HSpiceRead, METH_VARARGS, "Read hspice plot file.\n"},
    {NULL, NULL, 0, NULL} // Marks the end of this structure.
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_hspice_read",                      /* name of module */
    "HSPICE plot file import module.\n", /* module documentation, may be NULL */
    /* TODO: in future set this to 0 so that this module will work with
       sub-interpreters */
    -1, /* size of per-interpreter state of the module, or -1 if the module
           keeps state in global variables. */
    module_methods,
    NULL, /* Slots for multi phase initialization */
    NULL, /* Traversal function for GC */
    NULL, /* Clear function for clearing the module */
    NULL, /* Function for deallocating the module */
};

/* Module initialization
   Module name must be _hspice_read in compile and link */
PyMODINIT_FUNC PyInit__hspice_read() {
  PyObject *m = PyModule_Create(&module);

  /* For using NumPy */
  import_array();

  if (m == NULL)
    return NULL;

  // Initialize exceptions
  HSpiceParseError =
      PyErr_NewException("hspice_parser.HSpiceParseError", NULL, NULL);
  if (HSpiceParseError == NULL) {
    Py_DECREF(m);
    return NULL;
  }

  // Export to the module
  if (PyModule_AddObjectRef(m, "HSpiceParseError", HSpiceParseError) < 0) {
    Py_DECREF(HSpiceParseError);
    Py_CLEAR(HSpiceParseError);
    Py_DECREF(m);
    return NULL;
  }

  return m;
}

#define debugFile stdout

// Header character positions
#define blockHeaderSize 4
#define numOfVariablesPosition 0
#define numOfProbesPosition 4
#define numOfSweepsPosition 8
#define numOfSweepsEndPosition 12
#define postStartPosition1 16
#define postStartPosition2 20
#define numOfPostCharacters 4
#define dateStartPosition 88
#define dateEndPosition 112
#define titleStartPosition 24
#define sweepSizePosition 187
#define vectorDescriptionStartPosition 256
#define frequency 2
#define complex_var 1
#define real_var 0

// Perform endian swap on array of numbers. Arguments:
//   block    ... pointer to array of numbers
//   size     ... size of the array
//   itemSize ... size of one number in the array in bytes
void do_swap(char *block, int size, int itemSize) {
  int i;
  for (i = 0; i < size; i++) {
    int j;
    for (j = 0; j < itemSize / 2; j++) {
      char tmp = block[j];
      block[j] = block[itemSize - j - 1];
      block[itemSize - j - 1] = tmp;
    }
    block = block + itemSize;
  }
}

// Read block header. Returns:
//   -1 ... block header corrupted
//    0 ... success
//    1 ... eof
// Arguments:
//   ctx         ... parser context
//   blockHeader ... array of four integers consisting block header
//   size        ... size of items in block
int readBlockHeader(struct ParserContext *ctx, int *blockHeader, int size) {
  int num, blockSize;
  num = fread(blockHeader, sizeof(int), blockHeaderSize, ctx->f);
  if (num != blockHeaderSize) {
    if (num == 0 && feof(ctx->f)) {
      return 1; // Normal EOF
    }
    PyErr_Format(HSpiceParseError, "Failed to read block header from file %s.",
                 ctx->fileName);

    return -1; // Error.
  }

  // Block header check and swap.
  if (blockHeader[0] == 0x00000004 && blockHeader[2] == 0x00000004) {
    ctx->swap = false;
  } else if (blockHeader[0] == 0x04000000 && blockHeader[2] == 0x04000000) {
    ctx->swap = true;
  } else {
    PyErr_Format(HSpiceParseError, "Corrupted block header.");
    return -1;
  }

  if (ctx->swap) {
    do_swap((char *)blockHeader, blockHeaderSize, sizeof(int));
  }

  // Block size check
  blockSize = blockHeader[blockHeaderSize - 1];
  if (blockSize <= 0) {
    PyErr_Format(HSpiceParseError, "Block size %d is zero or negative.",
                 blockSize);
    return -1;
  }
  if (blockSize % size != 0) {
    PyErr_Format(HSpiceParseError, "Block size %d is misaligned for size %d.",
                 blockSize, size);
    return -1;
  }
  if (blockSize > 100000000) {
    PyErr_Format(HSpiceParseError, "Block size %d exceeds limit.", blockSize);
    return -1;
  }
  blockHeader[0] = blockHeader[blockHeaderSize - 1] / size;

  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Got block header, size=%d, bytes=%d, swap=%d\n",
            blockHeader[1], blockHeader[3], ctx->swap);
  }
  return 0;
}

// Read block data. Returns:
//   -1 ... reading failed
//   0  ... reading performed normally
// Arguments:
//   ctx         ... parser context
//   ptr         ... pointer to reserved space for data
//   offset      ... pointer to reserved space size,
//                   increased for current block size
//   itemSize    ... size of one item in block
//   numOfItems  ... number of items in block
//   swap        ... perform endian swap flag
int readBlockData(struct ParserContext *ctx, void *ptr, int *offset,
                  int itemSize, int numOfItems) {
  int num = fread(ptr, itemSize, numOfItems, ctx->f);
  if (num != numOfItems) {
    PyErr_Format(HSpiceParseError, "Failed to read block from file %s.",
                 ctx->fileName);
    return -1; // Error.
  }
  *offset = *offset + numOfItems;
  if (ctx->swap) {
    do_swap((char *)ptr, numOfItems, itemSize); // Endian swap.
  }
  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Got block data, item_size=%d, count=%d, bytes=%d\n",
            itemSize, numOfItems, itemSize * numOfItems);
  }
  return 0;
}

// Read block trailer. Returns:
//   -1 ... block trailer corrupted
//   0 ... reading performed normally
// Arguments:
//   ctx         ... parser context
//   swap        ... perform endian swap flag
//   header      ... block size from header
int readBlockTrailer(struct ParserContext *ctx, int header) {
  int trailer, num;
  num = fread(&trailer, sizeof(int), 1, ctx->f);
  if (num != 1) {
    PyErr_Format(HSpiceParseError, "Failed to read block trailer from file %s.",
                 ctx->fileName);
    return -1; // Error.
  }
  if (ctx->swap) {
    do_swap((char *)(&trailer), 1, sizeof(int)); // Endian swap.
  }

  // Block header and trailer match check.
  if (header != trailer) {
    PyErr_Format(HSpiceParseError, "Block header and trailer mismatch.");
    return -1; // Error.
  }
  if (ctx->debugMode >= 2) {
    fprintf(debugFile,
            "Got block trailer, itemsize=%ld, count=%d, bytes=%ld,\n",
            sizeof(int), 1, sizeof(int) * 1);
  }

  return 0;
}

// Reallocate space. Returns:
// 	 NULL    ... reallocation failed
//   pointer ... address of reallocated space
// Arguments:
//   debugMode   ... debug messages flag
//   ptr         ... pointer to already allocated space
//   size        ... new size in bytes
void *reallocate(int debugMode, void *ptr, int size) {
  // Allocate space for raw data.
  void *tmp = PyMem_Realloc(ptr, size);
  if (tmp == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to reallocate buffer space.");
  }
  return tmp;
}

// Read one file header block. Returns:
//    -1 ... error occured during reading the block
//    0 ... this was the last block
//    1 ... there is at least one more block left
// Arguments:
//   ctx       ... parser context
//   buf       ... pointer to header buffer,
//                 enlarged (reallocated) for current block
//   bufOffset ... pointer to buffer size, increased for current block size
int readHeaderBlock(struct ParserContext *ctx, char **buf, int *bufOffset) {
  char *tmpBuf;
  int error;
  int blockHeader[blockHeaderSize];

  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Reading header block @0x%lx\n", ftell(ctx->f));
  }

  // Get size of file header block.
  error = readBlockHeader(ctx, blockHeader, sizeof(char));
  if (error < 0) {
    return -1; // Error.
  }

  // Allocate space for buffer.
  if (blockHeader[0] > INT_MAX - *bufOffset - 1) {
    PyErr_Format(HSpiceParseError, "Buffer offset overflow.");
    return -1;
  }
  tmpBuf = reallocate(ctx->debugMode, *buf,
                      (*bufOffset + blockHeader[0] + 1) * sizeof(char));
  if (tmpBuf == NULL) {
    return -1; // Error.
  }
  *buf = tmpBuf;

  // Read file header block.
  error = readBlockData(ctx, *buf + *bufOffset, bufOffset, sizeof(char),
                        blockHeader[0]);
  if (error < 0) {
    return -1; // Error.
  }
  (*buf)[*bufOffset] = 0;

  // Read trailer of file header block.
  error = readBlockTrailer(ctx, blockHeader[blockHeaderSize - 1]);
  if (error < 0) {
    return -1; // Error.
  }

  if (strstr(*buf, "$&%#")) {
    return 0; // End of block.
  }

  return 1; // There is more.
}

static int parse_int(const char *str, int *out_val) {
  char *endptr;
  long val;
  if (str == NULL || *str == '\0') {
    return -1;
  }
  errno = 0;
  val = strtol(str, &endptr, 10);
  if (endptr == str) {
    return -1;
  }
  if (errno == ERANGE || val < INT_MIN || val > INT_MAX) {
    return -1;
  }
  while (*endptr != '\0') {
    if (*endptr != ' ' && *endptr != '\t' && *endptr != '\n' &&
        *endptr != '\r') {
      return -1;
    }
    endptr++;
  }
  *out_val = (int)val;
  return 0;
}

// Get sweep information from file header block. Returns:
//   -1 ... error occurred
//   0 ... performed normally
// Arguments:
//   debugMode   ... debug messages flag
//   sweep       ... acquired sweep parameter name, new reference created
//   buf         ... header string
//   sweepSize   ... acquired number of sweep points
//   sweepValues ... sweep points array, new reference created
//   faSweep     ... pointer to fast access structure for sweep array
int getSweepInfo(int debugMode, PyObject **sweep, char *buf, int *sweepSize,
                 PyArrayObject **sweepValues, struct FastArray *faSweep) {
  char *sweepName = NULL;
  npy_intp dims;

  if (debugMode >= 2) {
    fprintf(debugFile, "Reading sweep information.\n");
  }

  sweepName = strtok(NULL, " \t\n"); // Get sweep parameter name.
  if (sweepName == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to extract sweep name.");
    return -1;
  }
  *sweep = PyUnicode_InternFromString(sweepName);
  if (*sweep == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to create sweep name string.");
    return -1;
  }

  // Get number of sweep points.
  if (parse_int(&buf[sweepSizePosition], sweepSize) < 0) {
    PyErr_Format(HSpiceParseError,
                 "Failed to parse sweep size as valid integer.");
    return -1;
  }

  // Create array for sweep parameter values.
  dims = *sweepSize;
  *sweepValues = (PyArrayObject *)PyArray_SimpleNew(1, &dims, NPY_DOUBLE);
  if (*sweepValues == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to create array.");
    return -1;
  }

  // Prepare fast access structure.
  faSweep->data = PyArray_DATA(*sweepValues);
  faSweep->pos = PyArray_DATA(*sweepValues);
  faSweep->stride =
      PyArray_STRIDE(*sweepValues, PyArray_NDIM(*sweepValues) - 1);
  faSweep->length = PyArray_SIZE(*sweepValues);

  return 0;
}

// Read one data block. Returns:
//    -1 ... error occured during reading the block
//    0 ... this was the last block
//    1 ... there is at least one more block left
// Arguments:
//   ctx           ... parser context
//   rawData       ... pointer to data array,
//                     enlarged (reallocated) for current block
//   rawDataOffset ... pointer to data array size, increased for current block
//   size
int readDataBlock(struct ParserContext *ctx, float **rawData,
                  int *rawDataOffset) {
  int error;
  int blockHeader[blockHeaderSize];
  float *tmpRawData;
  double lastVal;

  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Reading data block @0x%lx\n", ftell(ctx->f));
  }

  // Get size of raw data block.
  error = readBlockHeader(ctx, blockHeader, sizeof(float));
  if (error == 1) {
    return 0; // Normal EOF / End of block.
  } else if (error < 0) {
    return -1; // Error.
  }

  // Allocate space for raw data.
  if (blockHeader[0] > (INT_MAX / (int)sizeof(float)) - *rawDataOffset) {
    PyErr_Format(HSpiceParseError, "Raw data offset overflow.");
    return -1;
  }
  tmpRawData = reallocate(ctx->debugMode, *rawData,
                          (*rawDataOffset + blockHeader[0]) * sizeof(float));
  if (tmpRawData == NULL) {
    return -1; // Error.
  }
  *rawData = tmpRawData;

  // Read raw data block.
  error = readBlockData(ctx, *rawData + *rawDataOffset, rawDataOffset,
                        sizeof(float), blockHeader[0]);
  if (error < 0) {
    return -1; // Error.
  }
  // Read trailer of file header block.
  error = readBlockTrailer(ctx, blockHeader[blockHeaderSize - 1]);
  if (error < 0) {
    return -1; // Error.
  }

  if (ctx->format == HSPICE_FORMAT_2013 || ctx->format == HSPICE_FORMAT_2001) {
    if (*rawDataOffset >= 2) {
      memcpy(&lastVal, *rawData + *rawDataOffset - 2, 8);
      if (ctx->swap) {
        do_swap((char *)&lastVal, 1, 8);
      }
      if (lastVal > 9e29) {
        return 0; // End of block.
      }
    }
  } else {
    if ((*rawData)[*rawDataOffset - 1] > 9e29) {
      return 0; // End of block.
    }
  }
  return 1; // There is more.
}

// Read one table for one sweep value. Returns:
//   -1 ... error occurred
//   0 ... performed normally
// Arguments:
//   ctx            ... parser context
//   sweep          ... sweep parameter name
//   numOfVariables ... number of variables in table
//   type           ... type of variables with exception of scale
//   numOfVectors   ... number of variables and probes in table
//   varSizes       ... array of vector sizes
//   faSweep        ... pointer to fast access structure for sweep array
//   tmpArray       ... array of pointers to arrays
//   faPtr          ... array of fast access structures for vector arrays
//   scale          ... scale name
//   name           ... array of vector names
//   dataList       ... list of data dictionaries
int readTable(struct ParserContext *ctx, PyObject *sweep, int numOfVariables,
              int type, int numOfVectors, int *varSizes,
              struct FastArray *faSweep, PyArrayObject **tmpArray,
              struct FastArray *faPtr, char *scale, char **name,
              PyObject *dataList) {
  int i, j, num, offset = 0, numOfColumns = numOfVectors, rowSize = 0,
                 tableBytes = 0, dataBytes = 0;
  int varSize;
  npy_intp dims;
  float *rawData = NULL;
  PyObject *data = NULL;
  char *bytePtr = NULL;
  double lastVal, val, re_d, im_d, dSweepVal;
  float re, im, fval, fSweepVal, re_f, im_f, fval_f;
  struct FastArray *faPos;
  int sweepHeaderSize = 0;

  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Reading table for one sweep point.\n");
  }

  // 1. Read raw data blocks via stream control
  do {
    num = readDataBlock(ctx, &rawData, &offset);
    if (num < 0) {
      goto readTableFailed;
    }
  } while (num > 0);

  data = PyDict_New(); // Create an empty dictionary.
  if (data == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to create data dictionary.");
    goto readTableFailed;
  }

  bytePtr = (char *)rawData;
  tableBytes = offset * sizeof(float);

  // 2. Clear out the dedicated termination block bytes from the payload
  // calculations
  if (ctx->format == HSPICE_FORMAT_2013) {
    if (tableBytes >= 8) {
      memcpy(&lastVal, (char *)rawData + tableBytes - 8, 8);
      if (ctx->swap) {
        do_swap((char *)&lastVal, 1, 8);
      }
      if (lastVal > 9e29) {
        tableBytes -= 8;
      }
    }
  } else {
    if (tableBytes >= 4) {
      tableBytes -= 4; // 9601, 9007, and 2001 use 4 or matching natural widths
    }
  }

  // Calculate total layout width for one circuit row
  for (j = 0; j < numOfVectors; j++) {
    rowSize += varSizes[j];
  }

  // 3. Clean and parse the dynamic Sweep Value Header (Specification §4.2)
  dataBytes = tableBytes;
  if (sweep != NULL) {
    if (ctx->format == HSPICE_FORMAT_2001) {
      sweepHeaderSize = 8; // 2001 format stores sweep value as an 8-byte double
    } else {
      sweepHeaderSize = 4; // 2013, 9601, 9007 store it as a 4-byte float
    }
    dataBytes -= sweepHeaderSize;
  }

  // Establish matrix entry dimension count
  num = dataBytes / rowSize;

  // Process and record the parameter sweep coordinate if applicable
  if (sweep != NULL) {
    if (ctx->format == HSPICE_FORMAT_2001) {
      memcpy(&dSweepVal, bytePtr, 8);
      if (ctx->swap) {
        do_swap((char *)&dSweepVal, 1, 8);
      }
      *((npy_double *)(faSweep->pos)) = dSweepVal;
    } else {
      memcpy(&fSweepVal, bytePtr, 4);
      if (ctx->swap) {
        do_swap((char *)&fSweepVal, 1, 4);
      }
      *((npy_double *)(faSweep->pos)) = (double)fSweepVal;
    }
    bytePtr += sweepHeaderSize;
    faSweep->pos = faSweep->pos + faSweep->stride;
  }

  // Increase number of columns if circuit variables are complex
  if (type == complex_var) {
    numOfColumns = numOfColumns + numOfVariables - 1;
  }

  // 4. Construct Destination NumPy Arrays
  for (i = 0; i < numOfVectors; i++) {
    dims = num;
    if (type == complex_var && i > 0 && i < numOfVariables) {
      tmpArray[i] = (PyArrayObject *)PyArray_SimpleNew(1, &dims, NPY_CDOUBLE);
    } else {
      tmpArray[i] = (PyArrayObject *)PyArray_SimpleNew(1, &dims, NPY_DOUBLE);
    }
    if (tmpArray[i] == NULL) {
      if (ctx->debugMode) {
        fprintf(debugFile, "HSpiceRead: failed to create array.\n");
      }
      for (j = 0; j < i + 1; j++) {
        Py_XDECREF(tmpArray[j]);
      }
      goto readTableFailed;
    }
  }

  for (i = 0; i < numOfVectors; i++) // Prepare fast access structures.
  {
    faPtr[i].data = PyArray_DATA(tmpArray[i]);
    faPtr[i].pos = PyArray_DATA(tmpArray[i]);
    faPtr[i].stride =
        PyArray_STRIDE(tmpArray[i], PyArray_NDIM(tmpArray[i]) - 1);
    faPtr[i].length = PyArray_SIZE(tmpArray[i]);
  }

  // 5. Populate Hybrid Arrays from Data Payload Slices
  if (ctx->format == HSPICE_FORMAT_2013) {
    for (i = 0; i < num; i++) {
      faPos = faPtr;
      for (j = 0; j < numOfVectors; j++) {
        varSize = varSizes[j];
        if (type == complex_var && j > 0 && j < numOfVariables) {
          memcpy(&re, bytePtr, 4);
          memcpy(&im, bytePtr + 4, 4);
          if (ctx->swap) {
            do_swap((char *)&re, 1, 4);
            do_swap((char *)&im, 1, 4);
          }
          *(npy_cdouble *)(faPos->pos) = npy_cpack(re, im);
        } else if (varSize == 8) {
          memcpy(&val, bytePtr, 8);
          if (ctx->swap) {
            do_swap((char *)&val, 1, 8);
          }
          *((double *)(faPos->pos)) = val;
        } else {
          memcpy(&fval, bytePtr, 4);
          if (ctx->swap) {
            do_swap((char *)&fval, 1, 4);
          }
          *((double *)(faPos->pos)) = (double)fval;
        }
        bytePtr += varSize;
        faPos->pos = faPos->pos + faPos->stride;
        faPos = faPos + 1;
      }
    }
  } else if (ctx->format == HSPICE_FORMAT_2001) {
    for (i = 0; i < num; i++) {
      faPos = faPtr;
      for (j = 0; j < numOfVectors; j++) {
        varSize = varSizes[j];
        if (type == complex_var && j > 0 && j < numOfVariables) {
          memcpy(&re_d, bytePtr, 8);
          memcpy(&im_d, bytePtr + 8, 8);
          if (ctx->swap) {
            do_swap((char *)&re_d, 1, 8);
            do_swap((char *)&im_d, 1, 8);
          }
          *(npy_cdouble *)(faPos->pos) = npy_cpack(re_d, im_d);
        } else {
          memcpy(&val, bytePtr, 8);
          if (ctx->swap) {
            do_swap((char *)&val, 1, 8);
          }
          *((double *)(faPos->pos)) = val;
        }
        bytePtr += varSize;
        faPos->pos = faPos->pos + faPos->stride;
        faPos = faPos + 1;
      }
    }
  } else {
    for (i = 0; i < num; i++) {
      faPos = faPtr;
      for (j = 0; j < numOfVectors; j++) {
        varSize = varSizes[j];
        if (type == complex_var && j > 0 && j < numOfVariables) {
          memcpy(&re_f, bytePtr, 4);
          memcpy(&im_f, bytePtr + 4, 4);
          if (ctx->swap) {
            do_swap((char *)&re_f, 1, 4);
            do_swap((char *)&im_f, 1, 4);
          }
          *(npy_cdouble *)(faPos->pos) = npy_cpack(re_f, im_f);
        } else {
          memcpy(&fval_f, bytePtr, 4);
          if (ctx->swap) {
            do_swap((char *)&fval_f, 1, 4);
          }
          *((double *)(faPos->pos)) = (double)fval_f;
        }
        bytePtr += varSize;
        faPos->pos = faPos->pos + faPos->stride;
        faPos = faPos + 1;
      }
    }
  }

  PyMem_Free(rawData);
  rawData = NULL;

  // 6. Map Parsed NumPy structures back to the PyDict Object Mapping
  num = PyDict_SetItemString(data, scale, (PyObject *)(tmpArray[0]));
  i = -1;
  if (num == 0) {
    for (i = 0; i < numOfVectors - 1; i++) {
      num = PyDict_SetItemString(data, name[i], (PyObject *)(tmpArray[i + 1]));
      if (num != 0) {
        break;
      }
    }
  }
  for (j = 0; j < numOfVectors; j++) {
    Py_XDECREF(tmpArray[j]);
  }
  if (num) {
    if (i == -1) {
      PyErr_Format(HSpiceParseError,
                   "Failed to insert vector %s into dictionary.", scale);
    } else {
      PyErr_Format(HSpiceParseError,
                   "Failed to insert vector %s into dictionary.", name[i]);
    }

    goto readTableFailed;
  }

  num = PyList_Append(dataList, data);
  if (num) {
    PyErr_Format(HSpiceParseError, "Failed to append table to the list "
                                   "of data dictionaries.");

    goto readTableFailed;
  }
  Py_XDECREF(data);
  data = NULL;

  if (ctx->debugMode >= 2) {
    fprintf(debugFile, "Finished reading one sweep point.\n");
  }

  return 0;

readTableFailed:
  PyMem_Free(rawData);
  Py_XDECREF(data);
  return -1;
}

// This is the first prototype version of HSpiceRead function for reading
// HSpice output files.
// TODO:
//   ascii format support
//   different vector types support (like voltage, current ..., although I do
//   not
//                                   know what it would be good for)
//   scale monotonity check
static PyObject *HSpiceRead(PyObject *self, PyObject *args) {
  const char *fileName;
  char *token, *buf = NULL, **name = NULL;
  int debugMode, num, numOfVectors, numOfVariables, type,
      sweepSize = 1, i = dateStartPosition - 1, offset = 0;
  int parsedProbes;
  struct FastArray faSweep, *faPtr = NULL;
  PyObject *date = NULL, *title = NULL, *scale = NULL, *sweep = NULL,
           *dataList = NULL, *sweeps = NULL, *tuple = NULL, *list = NULL;
  PyArrayObject *sweepValues = NULL, **tmpArray = NULL;
  int *varTypes = NULL;
  int *varSizes = NULL;
  struct ParserContext ctx;
  HSpiceFormat parsedFormat = HSPICE_FORMAT_UNKNOWN;

  // Get hspice_read() arguments.
  if (!PyArg_ParseTuple(args, "si", &fileName, &debugMode)) {
    return NULL;
  }

  if (debugMode) {
    fprintf(debugFile, "HSpiceRead: reading file %s.\n", fileName);
  }

  ctx.f = fopen(fileName, "rb"); // Open the file.
  if (ctx.f == NULL) {
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, fileName);
    goto failed;
  }
  ctx.fileName = fileName;
  ctx.debugMode = debugMode;
  ctx.format = HSPICE_FORMAT_2013;
  ctx.swap = 0;

  num = getc(ctx.f);
  ungetc(num, ctx.f);
  if (num == EOF) // Test if there is data in the file.
  {
    PyErr_Format(PyExc_ValueError, "File '%s' contains no data (0 bytes).",
                 fileName);
    goto failed;
  }
  if ((num & 0x000000ff) >= ' ') // Test if the file is in ascii format.
  {
    PyErr_Format(PyExc_ValueError,
                 "File '%s' is encoded in ASCII text format, "
                 "but this parser only supports HSPICE binary format.",
                 fileName);
    goto failed;
  }

  // Read file header blocks.
  do {
    num = readHeaderBlock(&ctx, &buf, &offset);
    if (num < 0) {
      goto failed;
    }
  } while (num > 0);

  if (offset < vectorDescriptionStartPosition + 1) {
    PyErr_Format(PyExc_ValueError, "File '%s' header is too short.", fileName);
    goto failed;
  }

  // Check version of post format.
  // --- Check version of post format (Specification §3.1) ---
  // We safely map out format definitions using the spec's offset positions
  // First check for Modern formats (24 characters, version string at position
  // 20)
  if (strncmp(&buf[postStartPosition2], "2013", numOfPostCharacters) == 0) {
    parsedFormat = HSPICE_FORMAT_2013;
  } else if (strncmp(&buf[postStartPosition2], "2001", numOfPostCharacters) ==
             0) {
    parsedFormat = HSPICE_FORMAT_2001;
  }
  // Fall back to checking Legacy formats (20 characters, version string at
  // position 16)
  else if (strncmp(&buf[postStartPosition1], "9601", numOfPostCharacters) ==
           0) {
    parsedFormat = HSPICE_FORMAT_9601;
  } else if (strncmp(&buf[postStartPosition1], "9007", numOfPostCharacters) ==
             0) {
    parsedFormat = HSPICE_FORMAT_9007;
  }

  // If it doesn't match any of the spec definitions, throw a ValueError
  if (parsedFormat == HSPICE_FORMAT_UNKNOWN) {
    // Extract a snapshot of what was actually there for debugging
    char hiddenVersion[5] = {0};
    strncpy(hiddenVersion, &buf[postStartPosition2], 4);

    PyErr_Format(PyExc_ValueError,
                 "HSpiceRead: Unsupported or corrupt HSPICE post format "
                 "version (found '%s'). "
                 "Expected '9007', '9601', '2001', or '2013'.",
                 hiddenVersion);
    goto failed;
  }

  // Assign back to context framework for downstream row routing tracking
  ctx.format = parsedFormat;

  buf[dateEndPosition] = 0;
  date =
      PyUnicode_InternFromString(&buf[dateStartPosition]); // Get creation date.
  if (date == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to create date string.");
    goto failed;
  }

  while (buf[i] == ' ') {
    i--;
  }
  buf[i + 1] = 0;
  title = PyUnicode_InternFromString(&buf[titleStartPosition]); // Get title.
  if (title == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to create title string.");
    goto failed;
  }

  buf[numOfSweepsEndPosition] = 0; // Check number of sweep parameters.
  if (parse_int(&buf[numOfSweepsPosition], &num) < 0) {
    PyErr_Format(HSpiceParseError,
                 "Failed to parse number of sweeps as valid integer.");
    goto failed;
  }
  if (num < 0 || num > 1) {
    PyErr_Format(PyExc_ValueError, "Only single dimension sweeps supported.");
    goto failed;
  }

  buf[numOfSweepsPosition] = 0; // Get number of vectors (variables and probes).
  if (parse_int(&buf[numOfProbesPosition], &parsedProbes) < 0) {
    PyErr_Format(HSpiceParseError,
                 "Failed to parse number of probes as valid integer.");
    goto failed;
  }
  buf[numOfProbesPosition] = 0;
  if (parse_int(&buf[numOfVariablesPosition], &numOfVariables) < 0) {
    PyErr_Format(HSpiceParseError,
                 "Failed to parse number of variables as valid integer.");
    goto failed;
  }
  if (parsedProbes < 0 || numOfVariables <= 0 ||
      parsedProbes > INT_MAX - numOfVariables) {
    if (debugMode) {
      fprintf(debugFile,
              "HSpiceRead: invalid probe or variable counts (probes=%d, "
              "variables=%d).\n",
              parsedProbes, numOfVariables);
    }
    goto failed;
  }
  numOfVectors = parsedProbes + numOfVariables;
  if (numOfVectors > INT_MAX / (int)sizeof(PyArrayObject *)) {
    PyErr_Format(PyExc_ValueError, "Total number of vectors is too large.");

    goto failed;
  }

  // Allocate space for types and sizes
  varTypes = (int *)PyMem_Malloc(numOfVectors * sizeof(int));
  if (varTypes == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to allocate vector types.");
    goto failed;
  }
  varSizes = (int *)PyMem_Malloc(numOfVectors * sizeof(int));
  if (varSizes == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to allocate vector sizes.");
    goto failed;
  }

  // Get type of variables. Scale is always real.
  token = strtok(&buf[vectorDescriptionStartPosition], " \t\n");
  if (token == NULL) {
    PyErr_Format(HSpiceParseError, "Failed to to extract vector description.");

    goto failed;
  }
  if (numOfVectors > 1) {
    if (parse_int(token, &varTypes[1]) < 0) {
      PyErr_Format(HSpiceParseError,
                   "Failed to parse vector type as valid integer.");
      goto failed;
    }
    type = varTypes[1];
  } else {
    if (parse_int(token, &varTypes[0]) < 0) {
      PyErr_Format(HSpiceParseError,
                   "Failed to parse vector type as valid integer.");
      goto failed;
    }
    type = varTypes[0];
  }
  if (type == frequency) {
    type = complex_var;
  } else {
    type = real_var;
  }

  for (i = 0; i < numOfVectors; i++) {
    token = strtok(NULL, " \t\n");
    if (token == NULL) {
      PyErr_Format(HSpiceParseError, "Failed to extract vector types/scale.");
      goto failed;
    }
    if (numOfVectors > 1) {
      if (i < numOfVectors - 2) {
        if (parse_int(token, &varTypes[i + 2]) < 0) {
          PyErr_Format(HSpiceParseError,
                       "Failed to parse vector type as valid integer.");
          goto failed;
        }
      } else if (i == numOfVectors - 2) {
        if (parse_int(token, &varTypes[0]) < 0) {
          PyErr_Format(HSpiceParseError,
                       "Failed to parse vector type as valid integer.");
          goto failed;
        }
      }
    }
  }

  scale = PyUnicode_InternFromString(token); // Get independent variable name.
  if (scale == NULL) {
    PyErr_Format(HSpiceParseError,
                 "Failed to create independent variable name string.");
    goto failed;
  }

  for (i = 0; i < numOfVectors; i++) {
    if (ctx.format == HSPICE_FORMAT_2013) {
      if (i == 0) {
        varSizes[i] = 8;
      } else if (type == complex_var && i > 0) {
        varSizes[i] = 8;
      } else {
        varSizes[i] = 4;
      }
    } else if (ctx.format == HSPICE_FORMAT_2001) {
      if (type == complex_var && i > 0) {
        varSizes[i] = 16;
      } else {
        varSizes[i] = 8;
      }
    } else {
      if (type == complex_var && i > 0) {
        varSizes[i] = 8;
      } else {
        varSizes[i] = 4;
      }
    }
    if (debugMode >= 2) {
      fprintf(debugFile, "Vector %d: type=%d, size=%d\n", i, varTypes[i],
              varSizes[i]);
    }
  }

  // Allocate space for pointers to vector names.
  name = (char **)PyMem_Malloc((numOfVectors - 1) * sizeof(char *));
  if (name == NULL) {
    PyErr_Format(PyExc_MemoryError,
                 "Failed to allocate pointers to vector names.");
    goto failed;
  }

  for (i = 0; i < numOfVectors - 1; i++) // Get vector names.
  {
    name[i] = strtok(NULL, " \t\n");
    if (name[i] == NULL) {
      PyErr_Format(HSpiceParseError, "Failed to extract vector names.");
      goto failed;
    }
  }

  // Process vector names: make name lowercase, remove v( in front of name
  for (i = 0; i < numOfVectors - 1; i++) {
    int j;
    for (j = 0; name[i][j]; j++) {
      if (name[i][j] >= 'A' && name[i][j] <= 'Z') {
        name[i][j] -= 'A' - 'a';
      }
    }
    if (name[i][0] == 'v' && name[i][1] == '(') {
      int len = 0;
      while (name[i][len]) {
        len++;
      }
      if (len > 2 && name[i][len - 1] == ')') {
        for (j = 2; j < len - 1; j++) {
          name[i][j - 2] = name[i][j];
        }
        name[i][len - 3] = 0;
      } else {
        for (j = 2; name[i][j]; j++) {
          name[i][j - 2] = name[i][j];
        }
        name[i][j - 2] = 0;
      }
    }
  }

  if (num == 1) // Get sweep information.
  {
    int num = getSweepInfo(debugMode, &sweep, buf, &sweepSize, &sweepValues,
                           &faSweep);
    if (num < 0) {
      goto failed;
    }
  }

  dataList = PyList_New(0); // Create an empty list for data dictionaries.
  if (dataList == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to create data list.");
    goto failed;
  }

  // Allocate space for pointers to arrays.
  tmpArray =
      (PyArrayObject **)PyMem_Malloc(numOfVectors * sizeof(PyArrayObject *));
  if (tmpArray == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to allocate pointers to arrays.");
    goto failed;
  }

  // Allocate space for fast array pointers.
  faPtr =
      (struct FastArray *)PyMem_Malloc(numOfVectors * sizeof(struct FastArray));
  if (faPtr == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to create array.");
    goto failed;
  }

  for (i = 0; i < sweepSize; i++) // Read i-th table.
  {
    num = readTable(&ctx, sweep, numOfVariables, type, numOfVectors, varSizes,
                    &faSweep, tmpArray, faPtr, token, name, dataList);
    if (num < 0) {
      goto failed;
    }
  }
  fclose(ctx.f);
  ctx.f = NULL;
  PyMem_Free(faPtr);
  faPtr = NULL;
  PyMem_Free(buf);
  buf = NULL;
  PyMem_Free(name);
  name = NULL;
  PyMem_Free(tmpArray);
  tmpArray = NULL;
  PyMem_Free(varTypes);
  varTypes = NULL;
  PyMem_Free(varSizes);
  varSizes = NULL;

  // Create sweeps tuple.
  if (sweep == NULL) {
    sweeps = PyTuple_Pack(3, Py_None, Py_None, dataList);
  } else {
    sweeps = PyTuple_Pack(3, sweep, sweepValues, dataList);
  }
  if (sweeps == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to create tuple with sweeps.");
    goto failed;
  }
  Py_XDECREF(sweep);
  sweep = NULL;
  Py_XDECREF(sweepValues);
  sweepValues = NULL;
  Py_XDECREF(dataList);
  dataList = NULL;

  // Prepare return tuple.
  tuple = PyTuple_Pack(6, sweeps, scale, Py_None, title, date, Py_None);
  if (tuple == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to create tuple with read data.");
    goto failed;
  }
  Py_XDECREF(date);
  date = NULL;
  Py_XDECREF(title);
  title = NULL;
  Py_XDECREF(scale);
  scale = NULL;
  Py_XDECREF(sweeps);
  sweeps = NULL;

  list = PyList_New(0); // Create an empty list.
  if (list == NULL) {
    PyErr_Format(PyExc_MemoryError, "Failed to create return list.");
    goto failed;
  }

  num = PyList_Append(list, tuple); // Insert tuple into return list.
  if (num) {
    PyErr_Format(PyExc_MemoryError, "Failed to append tuple to return list.");
    goto failed;
  }
  Py_XDECREF(tuple);

  return list;

failed: // Error occured. Close open file, release memory and python
        // references.
  if (ctx.f) {
    fclose(ctx.f);
  }
  PyMem_Free(buf);
  PyMem_Free(varTypes);
  PyMem_Free(varSizes);
  Py_XDECREF(date);
  Py_XDECREF(title);
  Py_XDECREF(scale);
  PyMem_Free(name);
  Py_XDECREF(sweep);
  Py_XDECREF(sweepValues);
  Py_XDECREF(dataList);
  PyMem_Free(tmpArray);
  PyMem_Free(faPtr);
  Py_XDECREF(sweeps);
  Py_XDECREF(tuple);
  Py_XDECREF(list);

  if (PyErr_Occurred()) {
    return NULL;
  }
  Py_INCREF(Py_None);
  return Py_None;
}
