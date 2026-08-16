# HALCON Operators Reference

> **Version:** HALCON 24.11 Progress Steady
>
> This document contains the HALCON operators that are used, planned to be used, or have been researched for the Thermal Monitoring System.
>
> This file serves as the project's HALCON API reference and should be updated whenever a new HALCON operator is introduced into the project.

---

# Table of Contents

- Drawing Objects
- Drawing Object Management
- Region Generation
- Domain Processing
- Display Operators
- Window Utilities

---

# Drawing Objects

Drawing Objects are interactive ROI objects managed by HALCON.
Unlike the `draw_*` operators, Drawing Objects are **non-blocking** and remain editable after creation.
Supported drawing objects include:
- Rectangle1
- Rectangle2
- Circle
- Circle Sector
- Ellipse
- Ellipse Sector
- Line
- XLD
- Text
Drawing Objects should always be used for interactive ROI editing.

---

# create_drawing_object_rectangle2

## Purpose

Create an interactive rotated rectangle.
Supports:
- Move
- Resize
- Rotation
The object becomes editable after attaching it to a window.

---

## Python
```python
draw_id = ha.create_drawing_object_rectangle2(
    row,
    column,
    phi,
    length1,
    length2
)
```

---

## Parameters

| Parameter | Type | Description |
|------------|------|-------------|
| row | float | Center row |
| column | float | Center column |
| phi | float | Rotation (radians) |
| length1 | float | First half axis |
| length2 | float | Second half axis |

---

## Returns

```python
HHandle
```
---

## Example

```python
draw_id = ha.create_drawing_object_rectangle2(
    250,
    400,
    0.0,
    60,
    30
)
```
---

## Notes
- Non-blocking
- Interactive
- Requires `attach_drawing_object_to_window()`
- Geometry can be queried using `get_drawing_object_params()`
- Region can be obtained using `get_drawing_object_iconic()`

---

# create_drawing_object_rectangle1

## Purpose
Create an interactive axis-aligned rectangle.
--
## Python
```python
draw_id = ha.create_drawing_object_rectangle1(
    row1,
    column1,
    row2,
    column2
)
```
---
## Parameters
| Parameter | Description |
|------------|-------------|
| row1 | Upper-left row |
| column1 | Upper-left column |
| row2 | Bottom-right row |
| column2 | Bottom-right column |
---
## Returns
```python
HHandle
```
---
# create_drawing_object_circle

## Purpose
Create an interactive circle.
---
## Python
```python
draw_id = ha.create_drawing_object_circle(
    row,
    column,
    radius
)
```
---
## Parameters
| Parameter | Description |
|------------|-------------|
| row | Center row |
| column | Center column |
| radius | Circle radius |
---

## Returns

```python
HHandle
```
---

# create_drawing_object_ellipse

## Purpose
Create an interactive ellipse.
---
## Python
```python
draw_id = ha.create_drawing_object_ellipse(
    row,
    column,
    phi,
    radius1,
    radius2
)
```
---

## Parameters

| Parameter | Description |
|------------|-------------|
| row | Center row |
| column | Center column |
| phi | Rotation |
| radius1 | First radius |
| radius2 | Second radius |
---
## Returns
```python
HHandle
```
---

# create_drawing_object_line

## Purpose
Create an interactive line.
---
## Python
```python
draw_id = ha.create_drawing_object_line(
    row1,
    column1,
    row2,
    column2
)
```
---

## Returns

```python
HHandle
```

---

# create_drawing_object_xld

## Purpose

Create an editable contour (polygon/XLD).

---

## Python

```python
draw_id = ha.create_drawing_object_xld(
    contour
)
```

---

## Returns

```python
HHandle
```

---

# create_drawing_object_circle_sector

## Purpose

Create an interactive circle sector.

---

## Python

```python
draw_id = ha.create_drawing_object_circle_sector(
    row,
    column,
    radius,
    start_angle,
    end_angle
)
```

---

# create_drawing_object_ellipse_sector

## Purpose

Create an interactive ellipse sector.

---

## Python

```python
draw_id = ha.create_drawing_object_ellipse_sector(
    row,
    column,
    phi,
    radius1,
    radius2,
    start_angle,
    end_angle
)
```

---

# create_drawing_object_text

## Purpose

Create an editable text drawing object.

---

## Python

```python
draw_id = ha.create_drawing_object_text(
    row,
    column,
    string
)
```

---

# Drawing Object Management

---

# attach_drawing_object_to_window

## Purpose

Attach a drawing object to an existing HALCON window.

The object becomes interactive after attachment.

---

## Python

```python
ha.attach_drawing_object_to_window(
    window_handle,
    draw_id
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| window_handle | HALCON window |
| draw_id | Drawing object |

---

## Example

```python
ha.attach_drawing_object_to_window(
    window,
    draw_id
)
```

---

## Notes

Required before the user can edit the ROI.

---

# detach_drawing_object_from_window

## Purpose

Remove a drawing object from a HALCON window.

---

## Python

```python
ha.detach_drawing_object_from_window(
    window_handle,
    draw_id
)
```

---

## Notes

Removes interaction but does **not** destroy the object.

---

# clear_drawing_object

## Purpose

Destroy a drawing object and free its resources.

---

## Python

```python
ha.clear_drawing_object(draw_id)
```

---

## Notes

Call when the ROI editor is closed.

---

# get_drawing_object_params

## Purpose

Read parameters from a drawing object.

---

## Python

```python
values = ha.get_drawing_object_params(
    draw_id,
    parameter_names
)
```

---

## Supported Parameters

### Geometry

```text
row
column
row1
column1
row2
column2
phi
length1
length2
radius
radius1
radius2
start_angle
end_angle
```

### Appearance

```text
color
line_width
line_style
marker_size
font
string
type
```

---

## Example

```python
row, col, phi, l1, l2 = ha.get_drawing_object_params(
    draw_id,
    [
        "row",
        "column",
        "phi",
        "length1",
        "length2"
    ]
)
```

---

## Returns

Tuple of requested values.

---

# set_drawing_object_params

## Purpose

Modify geometry or appearance of an existing drawing object.

---

## Python

```python
ha.set_drawing_object_params(
    draw_id,
    parameter_names,
    parameter_values
)
```

---

## Geometry Parameters

```text
row
column
phi
length1
length2
row1
column1
row2
column2
radius
radius1
radius2
start_angle
end_angle
```

---

## Appearance Parameters

```text
color
line_width
line_style
marker_size
font
string
```

---

## Example

### Change Color

```python
ha.set_drawing_object_params(
    draw_id,
    "color",
    "green"
)
```

### Change Line Width

```python
ha.set_drawing_object_params(
    draw_id,
    "line_width",
    3
)
```

### Change Multiple Parameters

```python
ha.set_drawing_object_params(
    draw_id,
    ["color", "line_width"],
    ["red", 2]
)
```

---

## Notes

- Window redraws automatically if attached.
- `line_style` must be set in a separate call.

---

# set_drawing_object_callback

## Purpose

Register callbacks for drawing object events.

---

## Python

```python
ha.set_drawing_object_callback(
    draw_id,
    event,
    callback
)
```

---

## Supported Events

```text
on_attach
on_detach
on_drag
on_resize
on_select
```

---

## Callback Signature

```cpp
Herror Callback(
    Hphandle DrawHandle,
    Hphandle WindowHandle,
    char* Event
)
```

---

## Notes

Do **NOT** call display operators inside callbacks.

Doing so may deadlock HALCON.

---

# get_drawing_object_iconic

## Purpose

Convert a drawing object into an iconic HALCON object (typically an `HRegion`).

---

## Python

```python
region = ha.get_drawing_object_iconic(
    draw_id
)
```

---

## Returns

```python
HObject
```

Typically:

```text
HRegion
```

---

## Example

```python
region = ha.get_drawing_object_iconic(draw_id)
```

---

## Typical Workflow

```python
draw = ha.create_drawing_object_rectangle2(...)

ha.attach_drawing_object_to_window(window, draw)

region = ha.get_drawing_object_iconic(draw)
```

---

# Region Generation

These operators create regions directly from geometry.

Unlike Drawing Objects, these are **not interactive**.

---

# gen_rectangle1

## Purpose

Create an axis-aligned region.

---

## Python

```python
region = ha.gen_rectangle1(
    row1,
    column1,
    row2,
    column2
)
```

---

# gen_rectangle2

## Purpose

Create a rotated rectangle region.

---

## Python

```python
region = ha.gen_rectangle2(
    row,
    column,
    phi,
    length1,
    length2
)
```

---

# gen_circle

## Purpose

Create a circular region.

---

## Python

```python
region = ha.gen_circle(
    row,
    column,
    radius
)
```

---

# gen_ellipse

## Purpose

Create an elliptical region.

---

## Python

```python
region = ha.gen_ellipse(
    row,
    column,
    phi,
    radius1,
    radius2
)
```

---

# gen_region_polygon

## Purpose

Create a polygon region.

---

## Python

```python
region = ha.gen_region_polygon(
    rows,
    columns
)
```

---

# gen_region_polygon_xld

## Purpose

Convert an XLD contour into a region.

---

## Python

```python
region = ha.gen_region_polygon_xld(
    contour
)
```

---

# Domain Processing

Domain operators restrict processing to specific image regions.

---

# reduce_domain

## Purpose

Restrict an image to a specified region.

Processing performed afterwards only affects the reduced domain.

---

## Python

```python
reduced_image = ha.reduce_domain(
    image,
    region
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| image | Input image |
| region | ROI region |

---

## Returns

```python
HObject
```

---

## Example

```python
region = ha.get_drawing_object_iconic(draw)

reduced = ha.reduce_domain(
    image,
    region
)
```

---

# Display Operators

These operators display images and graphics inside a HALCON window.

---

# disp_obj

Display any HALCON object.

```python
ha.disp_obj(
    object,
    window
)
```

---

# disp_region

Display a region.

```python
ha.disp_region(
    region,
    window
)
```

---

# disp_rectangle2

Display a rotated rectangle.

```python
ha.disp_rectangle2(
    window,
    row,
    column,
    phi,
    length1,
    length2
)
```

---

# disp_circle

```python
ha.disp_circle(
    window,
    row,
    column,
    radius
)
```

---

# disp_ellipse

```python
ha.disp_ellipse(
    window,
    row,
    column,
    phi,
    radius1,
    radius2
)
```

---

# disp_xld

```python
ha.disp_xld(
    contour,
    window
)
```

---

# Window Display Settings

---

# set_color

Set drawing color.

```python
ha.set_color(
    window,
    "red"
)
```

Supports:

- Color names
- RGB values

Example:

```python
ha.set_color(
    window,
    "#ff0000"
)
```

---

# set_gray

Set gray value output.

```python
ha.set_gray(
    window,
    255
)
```

---

# set_line_width

```python
ha.set_line_width(
    window,
    2
)
```

---

# set_line_style

```python
ha.set_line_style(
    window,
    [10,5]
)
```

Visible length:

```
10
```

Invisible length:

```
5
```

---

# set_draw

Choose region fill mode.

```python
ha.set_draw(
    window,
    "margin"
)
```

Common values:

```text
margin
fill
```

---

# set_colored

Enable automatic color cycling.

```python
ha.set_colored(
    window,
    12
)
```

---

# Window Coordinate Utilities

---

# convert_coordinates_window_to_image

Convert mouse coordinates to image coordinates.

```python
row, col = ha.convert_coordinates_window_to_image(
    window,
    window_row,
    window_col
)
```

---

# convert_coordinates_image_to_window

Convert image coordinates to window coordinates.

```python
window_row, window_col = ha.convert_coordinates_image_to_window(
    window,
    image_row,
    image_col
)
```

---


# Gray Value Statistics

These operators calculate temperature or gray-value statistics inside one or more regions.

For the Thermal Monitoring System, these operators are the primary methods used to calculate ROI temperatures.

---

# intensity

## Purpose

Calculate the **mean gray value** and **standard deviation** inside a region.

For thermal images (real images), this returns:

- Average temperature
- Temperature deviation

---

## Python

```python
mean, deviation = ha.intensity(
    region,
    image
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| region | ROI region |
| image | Gray/Real image |

---

## Returns

| Return | Description |
|---------|-------------|
| mean | Mean gray value / average temperature |
| deviation | Standard deviation |

---

## Example

```python
region = ha.get_drawing_object_iconic(draw)

mean_temp, std_dev = ha.intensity(
    region,
    temperature_image
)

print(mean_temp)
print(std_dev)
```

---

## Typical Usage

- ROI average temperature
- Temperature stability
- Alarm calculations
- Process monitoring

---

# min_max_gray

## Purpose

Determine the minimum and maximum gray values inside a region.

For thermal images this returns:

- Minimum temperature
- Maximum temperature
- Temperature range

---

## Python

```python
minimum, maximum, range_value = ha.min_max_gray(
    region,
    image,
    percent
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| region | ROI |
| image | Temperature image |
| percent | Percentage of pixels ignored (typically 0) |

---

## Returns

| Return | Description |
|---------|-------------|
| minimum | Minimum temperature |
| maximum | Maximum temperature |
| range_value | Maximum - Minimum |

---

## Example

```python
minimum, maximum, value_range = ha.min_max_gray(
    region,
    temperature_image,
    0
)
```

---

## Typical Usage

- Hot spot detection
- Cold spot detection
- Alarm generation

---

# gray_features

## Purpose

Calculate multiple gray-value features for one or more regions.

---

## Python

```python
values = ha.gray_features(
    regions,
    image,
    features
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| regions | Regions |
| image | Image |
| features | Requested features |

---

## Notes

Useful when several statistics are needed simultaneously.

Refer to HALCON documentation for supported feature names.

---

# gray_histo

## Purpose

Calculate histogram inside a region.

---

## Python

```python
histogram = ha.gray_histo(
    region,
    image
)
```

---

## Returns

Gray-value histogram.

---

## Typical Usage

- Temperature distribution
- Process analysis
- Statistical monitoring

---

# gray_histo_abs

## Purpose

Calculate absolute histogram.

---

## Python

```python
histogram = ha.gray_histo_abs(
    region,
    image
)
```

---

# gray_histo_range

## Purpose

Calculate histogram within a selected gray-value range.

---

## Python

```python
histogram = ha.gray_histo_range(
    region,
    image,
    minimum,
    maximum
)
```

---

# area_center_gray

## Purpose

Calculate area and gray-value weighted center.

---

## Python

```python
area, row, column = ha.area_center_gray(
    region,
    image
)
```

---

## Returns

- Area
- Center row
- Center column

---

# elliptic_axis_gray

## Purpose

Calculate orientation and major/minor axes using gray values.

---

## Python

```python
phi, radius1, radius2 = ha.elliptic_axis_gray(
    region,
    image
)
```

---

# select_gray

## Purpose

Select regions based on gray-value features.

---

## Python

```python
selected = ha.select_gray(
    regions,
    image,
    feature,
    operation,
    minimum,
    maximum
)
```

---

# Gray Morphology

Gray morphology modifies image gray values using neighborhood operations.

These operators work on images, **not regions**.

---

# dual_rank

## Purpose

Combined opening, median and closing.

---

## Python

```python
result = ha.dual_rank(
    image,
    mask
)
```

---

# gray_dilation

## Purpose

Perform gray-value dilation.

---

## Python

```python
result = ha.gray_dilation(
    image,
    structuring_element
)
```

---

# gray_erosion

## Purpose

Perform gray-value erosion.

---

## Python

```python
result = ha.gray_erosion(
    image,
    structuring_element
)
```

---

# gray_opening

## Purpose

Gray opening.

---

## Python

```python
result = ha.gray_opening(
    image,
    structuring_element
)
```

---

# gray_closing

## Purpose

Gray closing.

---

## Python

```python
result = ha.gray_closing(
    image,
    structuring_element
)
```

---

# gray_tophat

## Purpose

Gray-value top-hat transformation.

---

## Python

```python
result = ha.gray_tophat(
    image,
    structuring_element
)
```

---

# gray_bothat

## Purpose

Gray-value bottom-hat transformation.

---

## Python

```python
result = ha.gray_bothat(
    image,
    structuring_element
)
```

---

# gray_dilation_rect

## Purpose

Maximum filter using a rectangular mask.

---

## Python

```python
result = ha.gray_dilation_rect(
    image,
    mask_height,
    mask_width
)
```

---

## Parameters

| Parameter | Description |
|------------|-------------|
| mask_height | Rectangle height |
| mask_width | Rectangle width |

---

# gray_erosion_rect

## Purpose

Minimum filter using a rectangular mask.

---

## Python

```python
result = ha.gray_erosion_rect(
    image,
    mask_height,
    mask_width
)
```

---

# gray_range_rect

## Purpose

Calculate local gray-value range.

---

## Python

```python
result = ha.gray_range_rect(
    image,
    mask_height,
    mask_width
)
```

---

# gray_opening_rect

## Purpose

Opening with rectangular mask.

---

## Python

```python
result = ha.gray_opening_rect(
    image,
    mask_height,
    mask_width
)
```

---

# gray_closing_rect

## Purpose

Closing with rectangular mask.

---

## Python

```python
result = ha.gray_closing_rect(
    image,
    mask_height,
    mask_width
)
```

---

# gray_dilation_shape

## Purpose

Maximum filter with selectable mask shape.

---

## Python

```python
result = ha.gray_dilation_shape(
    image,
    mask_height,
    mask_width,
    mask_shape
)
```

---

## Supported Shapes

```text
rectangle
rhombus
octagon
```

---

# gray_erosion_shape

## Purpose

Minimum filter with selectable mask.

---

## Python

```python
result = ha.gray_erosion_shape(
    image,
    mask_height,
    mask_width,
    mask_shape
)
```

---

# gray_opening_shape

## Purpose

Opening with selectable mask.

---

## Python

```python
result = ha.gray_opening_shape(
    image,
    mask_height,
    mask_width,
    mask_shape
)
```

---

# gray_closing_shape

## Purpose

Closing with selectable mask.

---

## Python

```python
result = ha.gray_closing_shape(
    image,
    mask_height,
    mask_width,
    mask_shape
)
```

---

# Structuring Elements

Structuring elements are used by gray morphology operators.

---

# gen_disc_se

## Purpose

Generate a disk-shaped gray morphology structuring element.

---

## Python

```python
se = ha.gen_disc_se(
    radius,
    image_type
)
```

---

# read_gray_se

## Purpose

Load a gray morphology structuring element from disk.

Supports:

```
.gse
```

files.

---

## Python

```python
se = ha.read_gray_se(
    filename
)
```

---

## Returns

```python
HObject
```

---

## Example

```python
se = ha.read_gray_se(
    "filters/isod4"
)
```

---

# Common HALCON Workflows

---

# Create Interactive ROI

```python
draw = ha.create_drawing_object_rectangle2(
    250,
    400,
    0,
    60,
    30
)

ha.attach_drawing_object_to_window(
    window,
    draw
)
```

---

# Read ROI Geometry

```python
row, col, phi, l1, l2 = ha.get_drawing_object_params(
    draw,
    [
        "row",
        "column",
        "phi",
        "length1",
        "length2"
    ]
)
```

---

# Modify ROI Appearance

```python
ha.set_drawing_object_params(
    draw,
    [
        "color",
        "line_width"
    ],
    [
        "green",
        2
    ]
)
```

---

# Convert Drawing Object to Region

```python
region = ha.get_drawing_object_iconic(
    draw
)
```

---

# Generate Region Without Drawing Object

```python
region = ha.gen_rectangle2(
    row,
    column,
    phi,
    length1,
    length2
)
```

---

# Restrict Image to ROI

```python
reduced = ha.reduce_domain(
    image,
    region
)
```

---

# Calculate Mean Temperature

```python
mean_temp, deviation = ha.intensity(
    region,
    temperature_image
)
```

---

# Calculate Minimum and Maximum Temperature

```python
minimum, maximum, value_range = ha.min_max_gray(
    region,
    temperature_image,
    0
)
```

---

# Complete ROI Processing Example

```python
region = ha.get_drawing_object_iconic(draw)
mean_temp, std_dev = ha.intensity(
    region,
    temperature_image
)
minimum, maximum, temp_range = ha.min_max_gray(
    region,
    temperature_image,
    0
)
print(f"Mean : {mean_temp:.2f} °C")
print(f"Min  : {minimum:.2f} °C")
print(f"Max  : {maximum:.2f} °C")
print(f"Std  : {std_dev:.2f} °C")
print(f"Range: {temp_range:.2f} °C")
```
---

# Operator Selection Guide

| Task | Recommended Operator |
|------|-----------------------|
| Interactive ROI | `create_drawing_object_*` |
| ROI Editing | `get_drawing_object_params` |
| Modify ROI | `set_drawing_object_params` |
| ROI Callbacks | `set_drawing_object_callback` |
| Drawing Object → Region | `get_drawing_object_iconic` |
| Create Region from Geometry | `gen_rectangle*`, `gen_circle`, `gen_ellipse` |
| Restrict Processing | `reduce_domain` |
| Average Temperature | `intensity` |
| Standard Deviation | `intensity` |
| Minimum Temperature | `min_max_gray` |
| Maximum Temperature | `min_max_gray` |
| Temperature Range | `min_max_gray` |
| Histogram | `gray_histo` |
| Gray-value Features | `gray_features` |
| Region Selection | `select_gray` |
| Morphological Filtering | `gray_opening`, `gray_closing`, `gray_dilation`, `gray_erosion` |
| Load Structuring Element | `read_gray_se` |
| Generate Structuring Element | `gen_disc_se` |

---

# End of HALCON Operators Reference
```