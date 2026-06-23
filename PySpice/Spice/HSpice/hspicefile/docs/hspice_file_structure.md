# Synopsys HSPICE Binary Format Reference Manual

This document provides a comprehensive technical reference for the Synopsys HSPICE post-processor binary database format (generated via `.options post=1`). It details the file-level block chaining, block-level encapsulation headers, header-block metadata, and data-block hybrid precision record layout.

---

## 1. File-Level Layout

An HSPICE binary output database (such as `.tr0`, `.ac0`, or `.sw0` files) is structured as a contiguous sequence of self-describing **blocks**. Every file begins with exactly one **Header Block** (containing text-based metadata) followed by one or more **Data Blocks** containing the simulation records.

![Overall File Structure](file_structure.svg)

---

## 2. Block-Level Structure

Each individual block is encapsulated in a wrapper consisting of a **16-byte Block Head**, a variable-size **Data Payload**, and a **4-byte Block Tail**.

![Generic Block Encapsulation](block_structure.svg)

### 2.1 The Block Head (16 Bytes)
The block head consists of four 32-bit integers (`int`):
1.  **Endian Marker 1** (Offset `0`): Constant `0x00000004` (in file endianness).
2.  **Unused / Record Count** (Offset `4`): Typically `1` or unused.
3.  **Endian Marker 2** (Offset `8`): Constant `0x00000004` (in file endianness).
4.  **Payload Size** (Offset `12`): 32-bit integer indicating the exact size in bytes of the **Data Payload** section.

#### Endianness Determination
During file loading, a parser reads the block head. By comparing the byte ordering of Endian Marker 1:
*   If bytes read are `04 00 00 00` (value `4` in little-endian), the file matches the current standard standard little-endian host architecture (`swap = 0`).
*   If bytes read are `00 00 00 04` (value `4` in big-endian), byte-swapping must be performed on all subsequent integers, floats, and doubles read from the file (`swap = 1`).

### 2.2 The Block Tail (4 Bytes)
The block tail consists of a single 32-bit integer representing the size in bytes of the preceding Data Payload section. It acts as a trailing verification checksum. If this value does not match the Payload Size in the Block Head, the file block is considered corrupted.

---

## 3. The Header Block Payload

The payload of the very first block in the file contains plain text metadata (no newlines, pad spaces are used to maintain column alignment) and ends with the token `$&%#`.

### 3.1 Format Descriptor String (First 20 or 24 characters)
The very first characters of the Header Block form a fixed format descriptor string:
*   **Legacy Version (e.g. `2001`)**: 20 characters total:
    *   `0..3`: Number of monitored variables (e.g., `0005` for 5 variables).
    *   `4..15`: Padding zeros (`000000000000`).
    *   `16..19`: Format version identifier string (`2001`).
*   **Modern Version (e.g. `2013`)**: 24 characters total:
    *   `0..3`: Number of monitored variables (e.g., `0005`).
    *   `4..19`: Padding zeros (`0000000000000000`).
    *   `20..23`: Format version identifier string (`2013`).

### 3.2 Metadata Fields
Following the format descriptor, fixed character offsets define simulation properties:
*   **Title**: 64-character title of the simulation.
*   **Date**: 24-character creation date string.
*   **Sweeps Count**: Number of swept parameter values (`numOfSweeps`).
*   **Probes Count**: Number of probes/signals monitored.
*   **Variable Types**: The type code of each variable:
    *   `1`: Real floating-point signal (4-byte single-precision float).
    *   `2`: Complex floating-point signal (8-byte, composed of two 4-byte floats).
    *   `8`: Double-precision float (8-byte, used for scale/time variables).
*   **Variable Names**: Null/space-separated names of the variables (e.g., `TIME`, `v(in)`, `v(out)`, `i(vinput)`).
*   **Termination Marker**: The header block data payload ends with the characters `$&%#`.

---

## 4. The Data Block Payload & Record Layout

HSPICE writes simulation data points in a **Hybrid Precision Record Layout**. 

![Hybrid Precision Data Row](record_layout.svg)

### 4.1 Hybrid Precision Record Details
*   **Scale Variable**: The first variable in each record is the abscissa (e.g. `TIME` for transient analysis, `FREQUENCY` for AC, or the swept source value for DC sweep). In modern HSPICE output formats (e.g. version `2013`), the scale variable is always written as a **double-precision float (8-byte IEEE double)**.
*   **Circuit Variables**: Node voltages, branch currents, and device measurements are written as **single-precision floats (4-byte IEEE float)**, except for AC analysis where they are stored as complex pairs of single-precision floats (8 bytes total).
*   **Row Size calculation**:
    $$\text{Row Size (Bytes)} = 8 \text{ (scale double)} + \sum_{j=1}^{M} \text{Size of Variable } j$$
    For a transient analysis with 4 node voltage signals, the record length is:
    $$8 \text{ bytes (TIME)} + 4 \text{ bytes} \times 4 \text{ (signals)} = 24 \text{ bytes}$$

### 4.2 Record Iteration and EOF Chaining
1.  A parser reads the data blocks one by one.
2.  Each data block contains a variable number of data bytes (always a multiple of 4 bytes).
3.  The parser groups the payload bytes into data rows of size `Row Size`.
4.  Once a data block payload is fully consumed, the parser reads the next Block Head.
5.  If reading the Block Head fails due to EOF (`num == 0` and `feof(f)`), or if the final record contains the legacy termination float marker `> 9e29` (representing `1e30`), the parsing process finishes successfully.
