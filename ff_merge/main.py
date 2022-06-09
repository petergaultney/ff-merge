import argparse
from datetime import datetime
from functools import partial
from lxml import etree


def read_kml(fname: str):
    with open(fname, "rb") as f:
        return etree.fromstring(f.read())


def write_kml(fname: str, tree):
    etree.ElementTree(tree).write(fname, pretty_print=True)


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


def google_earth_merge(trees):
    """Trees must be pre-sorted"""
    base_tree = trees[0]
    others = trees[1:]

    base_track_pm = find_first_track_placemark(base_tree)
    for tree in others:
        next_track_pm = find_first_track_placemark(tree)
        assert next_track_pm is not None
        base_track_pm.addnext(next_track_pm)
        base_track_pm = next_track_pm

    remove_el(find_schemadata(base_tree))
    return base_tree


def merge_simplearraydata(base_tree, other_trees):
    def get_sads(tree):
        return tree.findall(
            "Document/ExtendedData/SchemaData/gx:SimpleArrayData", namespaces=FF_XML_NS
        )

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
            for val_el in sad.findall("gx:value", namespaces=FF_XML_NS):
                base_sad.append(val_el)
        num_sad_values = len(
            xpath(
                f"Document/ExtendedData/SchemaData/gx:SimpleArrayData[@name='{sad_name}']/gx:value"
            )(base_tree)
        )
        print(sad_name, num_rows)
        assert num_rows == num_sad_values, f"{sad_name}, {num_sad_values}, {num_rows}"

    return base_tree


def myflightbook_merge(merge_sad: bool, trees):
    base_tree = trees[0]
    other_trees = trees[1:]

    base_track_pm = base_tree.find("Document/Placemark/gx:Track", namespaces=FF_XML_NS)
    assert base_track_pm is not None
    for tree in other_trees:
        whens = tree.findall("Document/Placemark/gx:Track/when", namespaces=FF_XML_NS)
        coords = tree.findall(
            "Document/Placemark/gx:Track/gx:coord", namespaces=FF_XML_NS
        )
        for when, coord in zip(whens, coords):
            base_track_pm.append(when)
            base_track_pm.append(coord)
    if merge_sad:
        return merge_simplearraydata(base_tree, other_trees)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("filenames", nargs="+")
    parser.add_argument("--out", default="")
    parser.add_argument("-i", "--indices", type=int, nargs="*")
    parser.add_argument(
        "-m", "--merge", choices=["google", "mfb", "mfb-sad"], default="google"
    )
    args = parser.parse_args()

    all_trees = list(map(read_kml, args.filenames))

    merged = merge_ff_kmls(
        sort_and_select(args.indices, all_trees), merge_type=args.merge
    )

    outname = args.out or (
        "-".join(filter(None, ["merged", args.merge, ",".join(map(str, args.indices))]))
        + ".kml"
    )

    write_kml(outname, merged)


if __name__ == "__main__":
    main()
