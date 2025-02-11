import typing as ty
from datetime import datetime
from functools import partial
from logging import getLogger
from pathlib import Path

from lxml import etree

logger = getLogger(__name__)
clean_parser = etree.XMLParser(remove_blank_text=True)

FT_PER_M = 3.28084


def read_kml(fname: str):
    return etree.parse(fname, clean_parser).getroot()


def write_kml(fname: str, tree):
    etree.ElementTree(tree).write(fname, pretty_print=True, encoding="UTF-8", xml_declaration=True)


FF_XML_NS = {
    "gx": "http://www.google.com/kml/ext/2.2",
    None: "http://www.opengis.net/kml/2.2",
}


def ns_xpath(path: str, default_prefix="default"):
    parts = path.split("/")
    add_default_ns = lambda s: s if ":" in s else default_prefix + ":" + s
    return "/".join(map(add_default_ns, parts))


def xpath(path):
    NO_DEFAULT_NS = FF_XML_NS.copy()
    NO_DEFAULT_NS.pop(None)
    NO_DEFAULT_NS["default"] = FF_XML_NS[None]

    def _xpath(tree):
        nonlocal path
        path = ns_xpath(path)
        return tree.xpath(path, namespaces=NO_DEFAULT_NS)

    return _xpath


def mk_findall(path: str):
    return lambda tree: tree.findall(path, namespaces=FF_XML_NS)


def mk_find(path: str):
    return lambda tree: tree.find(path, namespaces=FF_XML_NS)


findall_placemarks = mk_findall("Document/Placemark")
find_schemadata = mk_find("Document/ExtendedData/SchemaData")
findall_coords = mk_findall("Document/Placemark/gx:Track/gx:coord")


def find_first_track_placemark(tree):
    for el in findall_placemarks(tree):
        if mk_find("gx:Track")(el) is not None:
            return el
    return None


def _fix_iso_dt(dt_s: str):
    """where these datetimes end with two decimal points and then a Z, which Python can't handle"""
    assert dt_s.endswith("Z")
    return dt_s.replace("Z", "0+00:00")


find_when = mk_find("Document/Placemark/gx:Track/when")
findall_when = mk_findall("Document/Placemark/gx:Track/when")


def get_track_start_time(tree):
    return datetime.fromisoformat(_fix_iso_dt(find_when(tree).text))


def mk_get_data(name: str):
    def get_data(tree):
        data = list(tree.findall("Document/ExtendedData/Data", namespaces=FF_XML_NS))
        for d in data:
            if d.attrib == {"name": name}:
                return d
        return None

    return get_data


def mk_extract_child_text(childname: str):
    def ex_ch_txt(el):
        return el.find(childname, namespaces=FF_XML_NS).text

    return ex_ch_txt


get_route = mk_get_data("routeWaypoints")
find_flight_title = mk_get_data("flightTitle")


def combine_flight_titles(titles):
    curr_title = ""
    for title in titles:
        if not curr_title:
            curr_title = title
        else:
            title_pieces = title.split(" - ")
            if curr_title.endswith(title_pieces[0]):
                curr_title = " - ".join([curr_title, *title_pieces[1:]])
            else:
                curr_title += title
    return curr_title


def remove_el(el):
    el.getparent().remove(el)


def merge_simplearraydata(base_tree, other_trees):
    def get_sads(tree):
        return tree.findall("Document/ExtendedData/SchemaData/gx:SimpleArrayData", namespaces=FF_XML_NS)

    def find_sad(tree, name):
        sads = get_sads(tree)
        for sad in sads:
            if sad.attrib == dict(name=name):
                return sad
        return None

    num_rows = len(findall_when(base_tree))
    for base_sad in get_sads(base_tree):
        sad_name = base_sad.attrib["name"]
        for tree in other_trees:
            sad = find_sad(tree, sad_name)
            assert sad is not None
            for i, val_el in enumerate(sad.findall("gx:value", namespaces=FF_XML_NS)):
                base_sad.append(val_el)
        num_sad_values = len(
            xpath(f"Document/ExtendedData/SchemaData/gx:SimpleArrayData[@name='{sad_name}']/gx:value")(
                base_tree
            )
        )
        assert num_rows == num_sad_values, f"{sad_name}, {num_sad_values}, {num_rows}"

    return base_tree


def filter_bad_items_from_track(tree):
    coords_to_drop = list()
    track = list()
    track = tree.find("Document/Placemark/gx:Track", namespaces=FF_XML_NS)
    whens = tree.findall("Document/Placemark/gx:Track/when", namespaces=FF_XML_NS)
    coords = tree.findall("Document/Placemark/gx:Track/gx:coord", namespaces=FF_XML_NS)
    assert len(whens) == len(coords)
    for i, (when, coord) in enumerate(zip(whens, coords)):
        if float(coord.text.split(" ")[2]) < 0:
            logger.warning(f"Dropping negative altitude coordinate at {i}: {coord.text}")
            coords_to_drop.append((i, when, coord))

    array_len = len(whens)
    reverse_dropped_indices = set([array_len - i for i, _, _ in coords_to_drop])

    for i, when, coord in reversed(coords_to_drop):
        track.remove(when)
        track.remove(coord)

    filtered_len = array_len - len(reverse_dropped_indices)

    def get_sads(tree):
        return tree.findall("Document/ExtendedData/SchemaData/gx:SimpleArrayData", namespaces=FF_XML_NS)

    for array_data in get_sads(tree):
        initial_len = len(array_data)
        name = array_data.attrib["name"]
        for i, val_el in enumerate(reversed(array_data.findall("gx:value", namespaces=FF_XML_NS))):
            if i in reverse_dropped_indices:
                logger.warning(
                    f"Dropping corresponding item in SimpleArrayData {name} at index {initial_len - i} with val {val_el.text}"
                )
                array_data.remove(val_el)
        assert len(array_data) == filtered_len
    return tree


def google_earth_merge(trees):
    """Trees must be pre-sorted"""
    base_tree = trees[0]
    other_trees = trees[1:]

    base_track_pm = find_first_track_placemark(base_tree)
    for tree in other_trees:
        next_track_pm = find_first_track_placemark(tree)
        assert next_track_pm is not None
        base_track_pm.addnext(next_track_pm)
        base_track_pm = next_track_pm

    return filter_bad_items_from_track(merge_simplearraydata(base_tree, other_trees))


def myflightbook_merge(merge_sad: bool, trees):
    base_tree = trees[0]
    other_trees = trees[1:]

    count_coords = lambda tree: len(findall_coords(tree))
    num_coords_before = sum(map(count_coords, trees))
    assert num_coords_before

    base_track_pm = base_tree.find("Document/Placemark/gx:Track", namespaces=FF_XML_NS)

    assert base_track_pm is not None
    for tree in other_trees:
        whens = tree.findall("Document/Placemark/gx:Track/when", namespaces=FF_XML_NS)
        coords = tree.findall("Document/Placemark/gx:Track/gx:coord", namespaces=FF_XML_NS)
        for when, coord in zip(whens, coords):
            base_track_pm.append(when)
            base_track_pm.append(coord)

    assert count_coords(base_tree) == num_coords_before
    if merge_sad:
        tree = merge_simplearraydata(base_tree, other_trees)
        return filter_bad_items_from_track(tree)

    # otherwise, remove all SimpleArrayData via the SchemaData top-level attribute
    remove_el(find_schemadata(base_tree))
    return base_tree


MERGES = {
    "google": google_earth_merge,
    "mfb": partial(myflightbook_merge, False),
    "mfb-sad": partial(myflightbook_merge, True),
}


def merge_ff_kmls(trees, merge_type: str = "google"):
    """Mutates the first (sorted order) tree"""
    merge = MERGES[merge_type]
    final_title = combine_flight_titles(
        map(mk_extract_child_text("value"), map(find_flight_title, trees))
    )
    base_tree = merge(trees)
    # it won't be valid for the merged set, and it's mostly not very interesting anyway
    find_flight_title(base_tree).find("value", namespaces=FF_XML_NS).text = final_title
    return base_tree


def sort_and_select(indices, trees):
    trees = sorted(trees, key=get_track_start_time)
    if indices:
        trees = [trees[i] for i in indices]
    assert len(trees), "Must have at least one KML tree to merge"
    return trees


def to_mfb_csv(fname: str, tree):
    import csv

    with open(fname, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, ["DATE", "LAT", "LON", "ALT", "AIRSPEED", "COURSE"])
        writer.writeheader()
        for when, coord, airspeed, course in zip(
            findall_when(tree),
            findall_coords(tree),
            xpath(f"Document/ExtendedData/SchemaData/gx:SimpleArrayData[@name='speed_kts']/gx:value")(
                tree
            ),
            xpath(f"Document/ExtendedData/SchemaData/gx:SimpleArrayData[@name='course']/gx:value")(tree),
        ):
            lon, lat, alt_m = coord.text.split(" ")
            writer.writerow(
                dict(
                    DATE=when.text.split(".")[0] + "Z",
                    LAT=float(lat),
                    LON=float(lon),
                    ALT=round(float(alt_m) * FT_PER_M, 2),
                    AIRSPEED=float(airspeed.text),
                    COURSE=float(course.text),
                )
            )


def merge_ff(
    *filenames: Path,
    out: str = "",
    indices: ty.Sequence[int] = tuple(),
    merge: ty.Literal["google", "mfb", "mfb-sad"] = "mfb-sad",
    old_dir: str = "old",
):
    all_trees = list(map(read_kml, filenames))

    merged = merge_ff_kmls(sort_and_select(indices, all_trees), merge_type=merge)

    base_name = "-".join(filter(None, ["merged", merge, ",".join(map(str, indices or list()))]))
    kmloutname = out or (base_name + ".kml")

    write_kml(kmloutname, merged)
    if merge == "mfb-sad":
        to_mfb_csv(base_name + ".csv", merged)

    if old_dir:
        Path(old_dir).mkdir(exist_ok=True)
        for fname in filenames:
            if (Path(".") / fname).exists():  # is in root directory
                Path(fname).resolve().rename(Path(old_dir) / fname)


def main():
    import argh

    argh.dispatch_command(merge_ff)


if __name__ == "__main__":
    main()
