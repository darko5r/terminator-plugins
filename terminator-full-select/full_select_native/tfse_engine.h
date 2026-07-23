#ifndef TFSE_ENGINE_H
#define TFSE_ENGINE_H

#include <stdint.h>

#if defined(__cplusplus)
extern "C" {
#endif

#if defined(_WIN32)
#define TFSE_API __declspec(dllexport)
#elif defined(__GNUC__) || defined(__clang__)
#define TFSE_API __attribute__((visibility("default")))
#else
#define TFSE_API
#endif

#define TFSE_ABI_VERSION UINT32_C(1)
#define TFSE_FRAME_ABI_VERSION UINT32_C(2)

enum tfse_feature_v1 {
    TFSE_FEATURE_SCALAR_C_V1 = UINT64_C(1) << 0,
    TFSE_FEATURE_GEOMETRY_V1 = UINT64_C(1) << 1,
    TFSE_FEATURE_ROW_SEGMENTS_V1 = UINT64_C(1) << 2,
    TFSE_FEATURE_FRAME_BATCH_V2 = UINT64_C(1) << 3,
};

typedef int32_t tfse_status_v1;

enum tfse_status_code_v1 {
    TFSE_STATUS_OK = 0,
    TFSE_STATUS_NULL_ARGUMENT = 1,
    TFSE_STATUS_ABI_MISMATCH = 2,
    TFSE_STATUS_STRUCT_TOO_SMALL = 3,
    TFSE_STATUS_NONFINITE = 4,
    TFSE_STATUS_RANGE = 5,
    TFSE_STATUS_OVERFLOW = 6,
    TFSE_STATUS_CAPACITY = 7,
    TFSE_STATUS_CONTENT_COUNT = 8,
};

struct tfse_abi_info_v1 {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t feature_flags;

    uint32_t geometry_input_size;
    uint32_t geometry_output_size;
    uint32_t viewport_input_size;
    uint32_t row_content_size;
    uint32_t segment_size;

    uint32_t reserved[7];
};

/*
 * All dimensions are signed so the engine can reproduce the Python
 * reference's max(value, 0) normalization at the ABI boundary.
 */
struct tfse_geometry_input_v1 {
    uint32_t struct_size;
    uint32_t abi_version;

    int64_t allocated_width;
    int64_t allocated_height;
    int64_t columns;
    int64_t rows;
    int64_t character_width;
    int64_t character_height;
    int64_t scale_factor;

    int64_t padding_left;
    int64_t padding_right;
    int64_t padding_top;
    int64_t padding_bottom;

    int64_t border_left;
    int64_t border_right;
    int64_t border_top;
    int64_t border_bottom;
};

struct tfse_geometry_output_v1 {
    uint32_t struct_size;
    uint32_t abi_version;

    int64_t x;
    int64_t y;
    int64_t width;
    int64_t height;

    int64_t allocated_width;
    int64_t allocated_height;
    int64_t columns;
    int64_t rows;
    int64_t character_width;
    int64_t character_height;

    int64_t mathematical_grid_width;
    int64_t mathematical_grid_height;
    int64_t remaining_width;
    int64_t remaining_height;
    int64_t right_remainder;
    int64_t bottom_remainder;

    int64_t scale_factor;

    int64_t padding_left;
    int64_t padding_right;
    int64_t padding_top;
    int64_t padding_bottom;

    int64_t border_left;
    int64_t border_right;
    int64_t border_top;
    int64_t border_bottom;

    int64_t style_left;
    int64_t style_right;
    int64_t style_top;
    int64_t style_bottom;
};

/*
 * This input contains only arithmetic state. Python remains responsible
 * for obtaining it from GTK/VTE on the GTK main thread.
 */
struct tfse_viewport_input_v1 {
    uint32_t struct_size;
    uint32_t abi_version;

    int64_t grid_x;
    int64_t grid_y;
    int64_t grid_width;
    int64_t grid_height;
    int64_t rows;
    int64_t columns;
    int64_t character_width;
    int64_t character_height;
    int64_t row_coordinate_offset;
    int64_t row_gap_px;
    int64_t horizontal_inset_px;

    double scroll_value;
};

/* One entry per potentially visible physical display row. */
struct tfse_row_content_v1 {
    uint32_t has_content;
    uint32_t reserved;
    int64_t start_column;
    int64_t end_column;
};

struct tfse_segment_v1 {
    int64_t absolute_row;
    int64_t display_row;
    int64_t start_column;
    int64_t end_column;
    int64_t x;
    int64_t y;
    int64_t width;
    int64_t height;
};

/*
 * Frame ABI v2 keeps every v1 structure and entry point valid. It batches the
 * geometry calculation and row-segment calculation behind one FFI boundary.
 * GTK/VTE access and text extraction remain on Python's GTK main thread.
 */
struct tfse_frame_abi_info_v2 {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t feature_flags;

    uint32_t frame_input_size;
    uint32_t frame_output_size;
    uint32_t geometry_input_size;
    uint32_t geometry_output_size;
    uint32_t viewport_input_size;
    uint32_t row_content_size;
    uint32_t segment_size;

    uint32_t reserved[5];
};

struct tfse_frame_input_v2 {
    uint32_t struct_size;
    uint32_t abi_version;

    struct tfse_geometry_input_v1 geometry;

    int64_t row_coordinate_offset;
    int64_t row_gap_px;
    int64_t horizontal_inset_px;
    int64_t overscan_rows;

    double scroll_value;
    uint64_t reserved[4];
};

struct tfse_frame_output_v2 {
    uint32_t struct_size;
    uint32_t abi_version;

    struct tfse_geometry_output_v1 geometry;

    uint64_t segment_count;
    uint64_t required_segment_capacity;
    uint64_t reserved[5];
};

TFSE_API uint32_t tfse_engine_abi_version(void);
TFSE_API uint64_t tfse_engine_feature_flags(void);
TFSE_API const char *tfse_engine_build_id(void);
TFSE_API const char *tfse_status_name(tfse_status_v1 status);

TFSE_API tfse_status_v1 tfse_query_abi_v1(
    struct tfse_abi_info_v1 *output
);

TFSE_API tfse_status_v1 tfse_calculate_geometry_v1(
    const struct tfse_geometry_input_v1 *input,
    struct tfse_geometry_output_v1 *output
);

/*
 * Query mode:
 *   segments == NULL and segment_capacity == 0
 *
 * Write mode requires capacity for the maximum possible segment count
 * (rows plus one when fractionally scrolled). No allocation occurs.
 */
TFSE_API tfse_status_v1 tfse_calculate_segments_v1(
    const struct tfse_viewport_input_v1 *input,
    const struct tfse_row_content_v1 *row_content,
    uint64_t row_content_count,
    struct tfse_segment_v1 *segments,
    uint64_t segment_capacity,
    uint64_t *segment_count
);

TFSE_API tfse_status_v1 tfse_query_frame_abi_v2(
    struct tfse_frame_abi_info_v2 *output
);

/*
 * Calculate geometry and bounded visible-row segments in one native call.
 * The caller supplies one content entry and one segment slot for every base
 * row, overscan row, and the possible fractional-scroll row. No allocation
 * and no GTK/VTE call occurs inside the engine.
 */
TFSE_API tfse_status_v1 tfse_calculate_frame_v2(
    const struct tfse_frame_input_v2 *input,
    const struct tfse_row_content_v1 *row_content,
    uint64_t row_content_count,
    struct tfse_segment_v1 *segments,
    uint64_t segment_capacity,
    struct tfse_frame_output_v2 *output
);

#if defined(__cplusplus)
}
#endif

#endif
