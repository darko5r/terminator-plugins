#include "tfse_engine.h"

#include <limits.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>

#define TFSE_DOUBLE_EXACT_INTEGER_LIMIT 9007199254740991.0

_Static_assert(sizeof(struct tfse_abi_info_v1) == 64, "ABI info layout");
_Static_assert(sizeof(struct tfse_geometry_input_v1) == 128, "geometry input layout");
_Static_assert(sizeof(struct tfse_geometry_output_v1) == 240, "geometry output layout");
_Static_assert(sizeof(struct tfse_viewport_input_v1) == 104, "viewport input layout");
_Static_assert(sizeof(struct tfse_row_content_v1) == 24, "row content layout");
_Static_assert(sizeof(struct tfse_segment_v1) == 64, "segment layout");
_Static_assert(sizeof(struct tfse_frame_abi_info_v2) == 64, "frame ABI info layout");
_Static_assert(sizeof(struct tfse_frame_input_v2) == 208, "frame input layout");
_Static_assert(sizeof(struct tfse_frame_output_v2) == 304, "frame output layout");

static int64_t tfse_nonnegative(int64_t value)
{
    return value > 0 ? value : INT64_C(0);
}

static int64_t tfse_min_i64(int64_t left, int64_t right)
{
    return left < right ? left : right;
}

static int tfse_add_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_add_overflow(left, right, result) ? 0 : 1;
#else
    if ((right > 0 && left > INT64_MAX - right) ||
        (right < 0 && left < INT64_MIN - right)) {
        return 0;
    }

    *result = left + right;
    return 1;
#endif
}

static int tfse_sub_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_sub_overflow(left, right, result) ? 0 : 1;
#else
    if ((right > 0 && left < INT64_MIN + right) ||
        (right < 0 && left > INT64_MAX + right)) {
        return 0;
    }

    *result = left - right;
    return 1;
#endif
}

static int tfse_mul_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_mul_overflow(left, right, result) ? 0 : 1;
#else
    if (left == 0 || right == 0) {
        *result = 0;
        return 1;
    }

    if ((left == INT64_MIN && right == -1) ||
        (right == INT64_MIN && left == -1)) {
        return 0;
    }

    if (left > 0) {
        if ((right > 0 && left > INT64_MAX / right) ||
            (right < 0 && right < INT64_MIN / left)) {
            return 0;
        }
    } else if ((right > 0 && left < INT64_MIN / right) ||
               (right < 0 && left < INT64_MAX / right)) {
        return 0;
    }

    *result = left * right;
    return 1;
#endif
}

static tfse_status_v1 tfse_validate_header_version(
    uint32_t struct_size,
    uint32_t abi_version,
    uint32_t expected_version,
    size_t required_size
)
{
    if (abi_version != expected_version) {
        return TFSE_STATUS_ABI_MISMATCH;
    }

    if ((uint64_t)struct_size < (uint64_t)required_size) {
        return TFSE_STATUS_STRUCT_TOO_SMALL;
    }

    return TFSE_STATUS_OK;
}

static tfse_status_v1 tfse_validate_header(
    uint32_t struct_size,
    uint32_t abi_version,
    size_t required_size
)
{
    return tfse_validate_header_version(
        struct_size,
        abi_version,
        TFSE_ABI_VERSION,
        required_size
    );
}

static tfse_status_v1 tfse_round_ties_even_nonnegative(
    double value,
    int64_t *result
)
{
    double base_value;
    double fraction;
    int64_t base;

    if (result == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    if (!isfinite(value)) {
        return TFSE_STATUS_NONFINITE;
    }

    if (value < 0.0 || value > TFSE_DOUBLE_EXACT_INTEGER_LIMIT) {
        return TFSE_STATUS_RANGE;
    }

    base_value = floor(value);
    base = (int64_t)base_value;
    fraction = value - base_value;

    if (fraction > 0.5 ||
        (fraction == 0.5 && (base & INT64_C(1)) != 0)) {
        if (!tfse_add_i64(base, INT64_C(1), &base)) {
            return TFSE_STATUS_OVERFLOW;
        }
    }

    *result = base;
    return TFSE_STATUS_OK;
}

uint32_t tfse_engine_abi_version(void)
{
    return TFSE_ABI_VERSION;
}

uint64_t tfse_engine_feature_flags(void)
{
    return TFSE_FEATURE_SCALAR_C_V1 |
           TFSE_FEATURE_GEOMETRY_V1 |
           TFSE_FEATURE_ROW_SEGMENTS_V1 |
           TFSE_FEATURE_FRAME_BATCH_V2;
}

const char *tfse_engine_build_id(void)
{
    return "tfse-native-c-v2-frame-batch";
}

const char *tfse_status_name(tfse_status_v1 status)
{
    switch (status) {
    case TFSE_STATUS_OK:
        return "ok";
    case TFSE_STATUS_NULL_ARGUMENT:
        return "null_argument";
    case TFSE_STATUS_ABI_MISMATCH:
        return "abi_mismatch";
    case TFSE_STATUS_STRUCT_TOO_SMALL:
        return "struct_too_small";
    case TFSE_STATUS_NONFINITE:
        return "nonfinite";
    case TFSE_STATUS_RANGE:
        return "range";
    case TFSE_STATUS_OVERFLOW:
        return "overflow";
    case TFSE_STATUS_CAPACITY:
        return "capacity";
    case TFSE_STATUS_CONTENT_COUNT:
        return "content_count";
    default:
        return "unknown";
    }
}

tfse_status_v1 tfse_query_abi_v1(struct tfse_abi_info_v1 *output)
{
    struct tfse_abi_info_v1 result = {0};
    tfse_status_v1 status;

    if (output == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    status = tfse_validate_header(
        output->struct_size,
        output->abi_version,
        sizeof(*output)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    result.struct_size = (uint32_t)sizeof(result);
    result.abi_version = TFSE_ABI_VERSION;
    result.feature_flags = tfse_engine_feature_flags();
    result.geometry_input_size = (uint32_t)sizeof(struct tfse_geometry_input_v1);
    result.geometry_output_size = (uint32_t)sizeof(struct tfse_geometry_output_v1);
    result.viewport_input_size = (uint32_t)sizeof(struct tfse_viewport_input_v1);
    result.row_content_size = (uint32_t)sizeof(struct tfse_row_content_v1);
    result.segment_size = (uint32_t)sizeof(struct tfse_segment_v1);

    *output = result;
    return TFSE_STATUS_OK;
}

tfse_status_v1 tfse_calculate_geometry_v1(
    const struct tfse_geometry_input_v1 *input,
    struct tfse_geometry_output_v1 *output
)
{
    struct tfse_geometry_output_v1 result = {0};
    tfse_status_v1 status;
    int64_t horizontal_style;
    int64_t vertical_style;

    if (input == NULL || output == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    status = tfse_validate_header(
        input->struct_size,
        input->abi_version,
        sizeof(*input)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    status = tfse_validate_header(
        output->struct_size,
        output->abi_version,
        sizeof(*output)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    if (input->scale_factor <= 0) {
        return TFSE_STATUS_RANGE;
    }

    result.struct_size = (uint32_t)sizeof(result);
    result.abi_version = TFSE_ABI_VERSION;

    result.allocated_width = tfse_nonnegative(input->allocated_width);
    result.allocated_height = tfse_nonnegative(input->allocated_height);
    result.columns = tfse_nonnegative(input->columns);
    result.rows = tfse_nonnegative(input->rows);
    result.character_width = tfse_nonnegative(input->character_width);
    result.character_height = tfse_nonnegative(input->character_height);
    result.scale_factor = input->scale_factor;

    result.padding_left = tfse_nonnegative(input->padding_left);
    result.padding_right = tfse_nonnegative(input->padding_right);
    result.padding_top = tfse_nonnegative(input->padding_top);
    result.padding_bottom = tfse_nonnegative(input->padding_bottom);

    result.border_left = tfse_nonnegative(input->border_left);
    result.border_right = tfse_nonnegative(input->border_right);
    result.border_top = tfse_nonnegative(input->border_top);
    result.border_bottom = tfse_nonnegative(input->border_bottom);

    if (!tfse_add_i64(
            result.border_left,
            result.padding_left,
            &result.style_left
        ) ||
        !tfse_add_i64(
            result.border_right,
            result.padding_right,
            &result.style_right
        ) ||
        !tfse_add_i64(
            result.border_top,
            result.padding_top,
            &result.style_top
        ) ||
        !tfse_add_i64(
            result.border_bottom,
            result.padding_bottom,
            &result.style_bottom
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (!tfse_mul_i64(
            result.columns,
            result.character_width,
            &result.mathematical_grid_width
        ) ||
        !tfse_mul_i64(
            result.rows,
            result.character_height,
            &result.mathematical_grid_height
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (!tfse_add_i64(
            result.style_left,
            result.style_right,
            &horizontal_style
        ) ||
        !tfse_add_i64(
            result.style_top,
            result.style_bottom,
            &vertical_style
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (result.allocated_width > horizontal_style) {
        result.width = result.allocated_width - horizontal_style;
    }

    if (result.allocated_height > vertical_style) {
        result.height = result.allocated_height - vertical_style;
    }

    result.width = tfse_min_i64(
        result.mathematical_grid_width,
        result.width
    );
    result.height = tfse_min_i64(
        result.mathematical_grid_height,
        result.height
    );

    result.remaining_width = result.allocated_width - result.width;
    result.remaining_height = result.allocated_height - result.height;

    result.x = tfse_min_i64(result.style_left, result.remaining_width);
    result.y = tfse_min_i64(result.style_top, result.remaining_height);

    result.right_remainder = result.remaining_width - result.x;
    result.bottom_remainder = result.remaining_height - result.y;

    *output = result;
    return TFSE_STATUS_OK;
}

tfse_status_v1 tfse_calculate_segments_v1(
    const struct tfse_viewport_input_v1 *input,
    const struct tfse_row_content_v1 *row_content,
    uint64_t row_content_count,
    struct tfse_segment_v1 *segments,
    uint64_t segment_capacity,
    uint64_t *segment_count
)
{
    tfse_status_v1 status;
    int64_t rows;
    int64_t columns;
    int64_t character_width;
    int64_t character_height;
    int64_t adjustment_top_row;
    int64_t top_absolute_row;
    int64_t pixel_scroll_offset;
    int64_t row_gap;
    int64_t upper_gap;
    int64_t band_height;
    int64_t grid_bottom;
    double adjustment_floor;
    double fractional_row;
    double scaled_fraction;
    uint64_t iteration_count;
    uint64_t output_count = 0;
    uint64_t display_row;

    if (input == NULL || segment_count == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    *segment_count = 0;

    status = tfse_validate_header(
        input->struct_size,
        input->abi_version,
        sizeof(*input)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    if (!isfinite(input->scroll_value)) {
        return TFSE_STATUS_NONFINITE;
    }

    if (input->scroll_value < -TFSE_DOUBLE_EXACT_INTEGER_LIMIT ||
        input->scroll_value > TFSE_DOUBLE_EXACT_INTEGER_LIMIT) {
        return TFSE_STATUS_RANGE;
    }

    rows = tfse_nonnegative(input->rows);
    columns = tfse_nonnegative(input->columns);
    character_width = tfse_nonnegative(input->character_width);
    character_height = tfse_nonnegative(input->character_height);

    if (rows == 0 || columns == 0 ||
        character_width == 0 || character_height == 0) {
        return TFSE_STATUS_OK;
    }

    adjustment_floor = floor(input->scroll_value);
    adjustment_top_row = (int64_t)adjustment_floor;

    if (!tfse_add_i64(
            adjustment_top_row,
            input->row_coordinate_offset,
            &top_absolute_row
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    fractional_row = input->scroll_value - adjustment_floor;
    scaled_fraction = fractional_row * (double)character_height;

    status = tfse_round_ties_even_nonnegative(
        scaled_fraction,
        &pixel_scroll_offset
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    if (pixel_scroll_offset >= character_height) {
        if (!tfse_add_i64(
                top_absolute_row,
                INT64_C(1),
                &top_absolute_row
            )) {
            return TFSE_STATUS_OVERFLOW;
        }

        pixel_scroll_offset = 0;
    }

    row_gap = tfse_nonnegative(input->row_gap_px);

    if (row_gap >= character_height) {
        row_gap = character_height - 1;
    }

    upper_gap = row_gap / 2;
    band_height = character_height - row_gap;

    iteration_count = (uint64_t)rows;

    if (pixel_scroll_offset != 0) {
        if (iteration_count == UINT64_MAX) {
            return TFSE_STATUS_OVERFLOW;
        }

        iteration_count += UINT64_C(1);
    }

    if (iteration_count > (uint64_t)SIZE_MAX) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (row_content_count < iteration_count) {
        *segment_count = iteration_count;
        return TFSE_STATUS_CONTENT_COUNT;
    }

    if (row_content == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    if (segments == NULL && segment_capacity != 0) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    if (segments != NULL && segment_capacity < iteration_count) {
        *segment_count = iteration_count;
        return TFSE_STATUS_CAPACITY;
    }

    if (!tfse_add_i64(
            input->grid_y,
            tfse_nonnegative(input->grid_height),
            &grid_bottom
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    for (display_row = 0; display_row < iteration_count; ++display_row) {
        const struct tfse_row_content_v1 *content = &row_content[display_row];
        struct tfse_segment_v1 segment;
        int64_t content_width;
        int64_t raw_width;
        int64_t maximum_inset;
        int64_t horizontal_inset;
        int64_t start_pixel;
        int64_t row_pixel;
        int64_t segment_bottom;
        int64_t display_row_i64 = (int64_t)display_row;

        if (content->has_content == 0) {
            continue;
        }

        if (content->start_column < 0 ||
            content->end_column <= content->start_column ||
            content->end_column > columns) {
            return TFSE_STATUS_RANGE;
        }

        content_width = content->end_column - content->start_column;

        if (!tfse_mul_i64(content_width, character_width, &raw_width)) {
            return TFSE_STATUS_OVERFLOW;
        }

        maximum_inset = (raw_width - 1) / 2;
        horizontal_inset = tfse_nonnegative(input->horizontal_inset_px);
        horizontal_inset = tfse_min_i64(horizontal_inset, maximum_inset);

        if (!tfse_mul_i64(
                content->start_column,
                character_width,
                &start_pixel
            ) ||
            !tfse_add_i64(start_pixel, input->grid_x, &start_pixel) ||
            !tfse_add_i64(start_pixel, horizontal_inset, &segment.x)) {
            return TFSE_STATUS_OVERFLOW;
        }

        if (!tfse_mul_i64(display_row_i64, character_height, &row_pixel) ||
            !tfse_add_i64(row_pixel, input->grid_y, &row_pixel) ||
            !tfse_sub_i64(row_pixel, pixel_scroll_offset, &row_pixel) ||
            !tfse_add_i64(row_pixel, upper_gap, &segment.y)) {
            return TFSE_STATUS_OVERFLOW;
        }

        if (!tfse_mul_i64(horizontal_inset, INT64_C(2), &start_pixel) ||
            !tfse_sub_i64(raw_width, start_pixel, &segment.width)) {
            return TFSE_STATUS_OVERFLOW;
        }

        if (segment.width <= 0) {
            continue;
        }

        if (!tfse_add_i64(segment.y, band_height, &segment_bottom)) {
            return TFSE_STATUS_OVERFLOW;
        }

        if (segment_bottom <= input->grid_y || segment.y >= grid_bottom) {
            continue;
        }

        if (!tfse_add_i64(
                top_absolute_row,
                display_row_i64,
                &segment.absolute_row
            )) {
            return TFSE_STATUS_OVERFLOW;
        }

        segment.display_row = display_row_i64;
        segment.start_column = content->start_column;
        segment.end_column = content->end_column;
        segment.height = band_height;

        if (segments != NULL) {
            segments[output_count] = segment;
        }

        output_count += UINT64_C(1);
    }

    *segment_count = output_count;
    return TFSE_STATUS_OK;
}

tfse_status_v1 tfse_query_frame_abi_v2(
    struct tfse_frame_abi_info_v2 *output
)
{
    struct tfse_frame_abi_info_v2 result = {0};
    tfse_status_v1 status;

    if (output == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    status = tfse_validate_header_version(
        output->struct_size,
        output->abi_version,
        TFSE_FRAME_ABI_VERSION,
        sizeof(*output)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    result.struct_size = (uint32_t)sizeof(result);
    result.abi_version = TFSE_FRAME_ABI_VERSION;
    result.feature_flags = tfse_engine_feature_flags();
    result.frame_input_size = (uint32_t)sizeof(struct tfse_frame_input_v2);
    result.frame_output_size = (uint32_t)sizeof(struct tfse_frame_output_v2);
    result.geometry_input_size = (uint32_t)sizeof(struct tfse_geometry_input_v1);
    result.geometry_output_size = (uint32_t)sizeof(struct tfse_geometry_output_v1);
    result.viewport_input_size = (uint32_t)sizeof(struct tfse_viewport_input_v1);
    result.row_content_size = (uint32_t)sizeof(struct tfse_row_content_v1);
    result.segment_size = (uint32_t)sizeof(struct tfse_segment_v1);

    *output = result;
    return TFSE_STATUS_OK;
}

tfse_status_v1 tfse_calculate_frame_v2(
    const struct tfse_frame_input_v2 *input,
    const struct tfse_row_content_v1 *row_content,
    uint64_t row_content_count,
    struct tfse_segment_v1 *segments,
    uint64_t segment_capacity,
    struct tfse_frame_output_v2 *output
)
{
    struct tfse_frame_output_v2 result = {0};
    struct tfse_geometry_output_v1 geometry = {
        .struct_size = sizeof(geometry),
        .abi_version = TFSE_ABI_VERSION,
    };
    struct tfse_viewport_input_v1 viewport = {
        .struct_size = sizeof(viewport),
        .abi_version = TFSE_ABI_VERSION,
    };
    tfse_status_v1 status;
    int64_t base_rows;
    int64_t overscan_rows;
    int64_t frame_rows;
    int64_t allocated_content_bottom;
    int64_t grid_bottom;
    int64_t overscan_height;
    int64_t overscan_limit_bottom;
    int64_t clip_bottom;
    uint64_t segment_count = 0;

    if (input == NULL || output == NULL) {
        return TFSE_STATUS_NULL_ARGUMENT;
    }

    status = tfse_validate_header_version(
        input->struct_size,
        input->abi_version,
        TFSE_FRAME_ABI_VERSION,
        sizeof(*input)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    status = tfse_validate_header_version(
        output->struct_size,
        output->abi_version,
        TFSE_FRAME_ABI_VERSION,
        sizeof(*output)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    status = tfse_validate_header(
        input->geometry.struct_size,
        input->geometry.abi_version,
        sizeof(input->geometry)
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    if (input->reserved[0] != 0 ||
        input->reserved[1] != 0 ||
        input->reserved[2] != 0 ||
        input->reserved[3] != 0) {
        return TFSE_STATUS_RANGE;
    }

    status = tfse_calculate_geometry_v1(
        &input->geometry,
        &geometry
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    base_rows = tfse_nonnegative(geometry.rows);
    overscan_rows = tfse_nonnegative(input->overscan_rows);

    if (!tfse_add_i64(base_rows, overscan_rows, &frame_rows)) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (!tfse_add_i64(geometry.y, geometry.height, &grid_bottom) ||
        !tfse_sub_i64(
            geometry.allocated_height,
            geometry.style_bottom,
            &allocated_content_bottom
        ) ||
        !tfse_mul_i64(
            overscan_rows,
            geometry.character_height,
            &overscan_height
        ) ||
        !tfse_add_i64(
            grid_bottom,
            overscan_height,
            &overscan_limit_bottom
        )) {
        return TFSE_STATUS_OVERFLOW;
    }

    if (allocated_content_bottom < geometry.y) {
        allocated_content_bottom = geometry.y;
    }

    clip_bottom = tfse_min_i64(
        allocated_content_bottom,
        overscan_limit_bottom
    );

    if (clip_bottom < geometry.y) {
        clip_bottom = geometry.y;
    }

    viewport.grid_x = geometry.x;
    viewport.grid_y = geometry.y;
    viewport.grid_width = geometry.width;
    viewport.grid_height = clip_bottom - geometry.y;
    viewport.rows = frame_rows;
    viewport.columns = geometry.columns;
    viewport.character_width = geometry.character_width;
    viewport.character_height = geometry.character_height;
    viewport.row_coordinate_offset = input->row_coordinate_offset;
    viewport.row_gap_px = input->row_gap_px;
    viewport.horizontal_inset_px = input->horizontal_inset_px;
    viewport.scroll_value = input->scroll_value;

    status = tfse_calculate_segments_v1(
        &viewport,
        row_content,
        row_content_count,
        segments,
        segment_capacity,
        &segment_count
    );

    if (status != TFSE_STATUS_OK) {
        return status;
    }

    result.struct_size = (uint32_t)sizeof(result);
    result.abi_version = TFSE_FRAME_ABI_VERSION;
    result.geometry = geometry;
    result.segment_count = segment_count;

    if (frame_rows == INT64_MAX) {
        return TFSE_STATUS_OVERFLOW;
    }

    result.required_segment_capacity = (uint64_t)(frame_rows + 1);

    *output = result;
    return TFSE_STATUS_OK;
}
