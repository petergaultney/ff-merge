import argparse
from datetime import datetime
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


def find_first_track_placemark(tree):
    for el in tree.findall("Document/Placemark", namespaces=FF_XML_NS):
        if el.find("gx:Track", namespaces=FF_XML_NS) is not None:
            return el
    return None


def find_schema_data(tree):
    return tree.findall("Document/ExtendedData/SchemaData", namespaces=FF_XML_NS)


def _fix_iso_dt(dt_s: str):
    """where these datetimes end with two decimal points and then a Z, which Python can't handle"""
    assert dt_s.endswith("Z")
    return dt_s.replace("Z", "0+00:00")


def get_track_start_time(tree):
    start = tree.find("Document/Placemark/gx:Track/when", namespaces=FF_XML_NS)
    return datetime.fromisoformat(_fix_iso_dt(start.text))


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


def merge_ff_kmls(trees):
    """Mutates the first (sorted order) tree"""
    trees = sorted(trees, key=get_track_start_time)
    assert len(trees), "Must have at least one KML tree to merge"
    final_title = combine_flight_titles(
        map(mk_extract_child_text("value"), map(find_flight_title, trees))
    )
    base_tree = trees[0]
    others = trees[1:]

    base_track_pm = find_first_track_placemark(base_tree)
    for tree in others:
        next_track_pm = find_first_track_placemark(tree)
        assert next_track_pm is not None
        base_track_pm.addnext(next_track_pm)
        base_track_pm = next_track_pm

    remove_el(find_schema_data(base_tree)[0])
    # it won't be valid for the merged set, and it's mostly not very interesting anyway

    find_flight_title(base_tree).find("value", namespaces=FF_XML_NS).text = final_title

    return base_tree


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("filenames", nargs="+")
    parser.add_argument("--out", default="merged.kml")
    args = parser.parse_args()

    all_trees = list(map(read_kml, args.filenames))

    merged = merge_ff_kmls(all_trees)

    write_kml(args.out, merged)


if __name__ == "__main__":
    main()
