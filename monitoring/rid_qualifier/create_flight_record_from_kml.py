#!/usr/bin/env python

# A file to generate Flight Records from KML.
import argparse
import datetime
import json
import math
import s2sphere
import os
import uuid
from datetime import datetime, timedelta
from shapely.geometry import LineString, Point, Polygon
from monitoring.monitorlib import kml
from monitoring.monitorlib.geo import flatten, unflatten
from monitoring.rid_qualifier.utils import FlightDetails, FullFlightRecord
from monitoring.monitorlib.rid import RIDHeight, RIDAircraftState, RIDAircraftPosition, RIDFlightDetails, LatLngPoint
from typing import List

# TODO: Need to know value for ALTITUDE_AGL
ALTITUDE_AGL = 50.0
STATE_INCREMENT_SECONDS = 1

        
def get_flight_coordinates(input_coordinates):
    # Reverse the Lng,Lat from KML to Lat,Lng for processing.
    return [(p[1], p[0], p[2]) for p in input_coordinates]


def get_distance_travelled():
    # TODO: should be calculated based on speed m/s, hardcoding to 2m/s for now.
    return 2


def get_distance_between_two_points(flatten_point1, flatten_point2):
    x1, y1 = flatten_point1
    x2, y2 = flatten_point2
    return ((((x2 - x1 )**2) + ((y2-y1)**2) )**0.5)


def check_if_vertex_is_correct(point1, point2, point3, flight_distance):
    x1, y1 = point1
    x2, y2 = point2
    x3, y3 = point3
    points_distance_difference = math.sqrt(
        (x2-x1)*(x2-x1) + (y2-y1)*(y2-y1)) - math.sqrt((x2-x3)*(x2-x3) + (y2-y3)*(y2-y3))
    assert abs(
        points_distance_difference - flight_distance
        ) < 0.2, f'Generated vertex is not correct. {point1}, {point2}, {point3}, {flight_distance}'


def get_flight_state_vertices(flatten_points, flattened_speed_polygons, all_polygon_speeds):
    """Get flight state vertices at flight distance's m/s interval.
    Args:
        flatten_points: A list of x,y coordinates on flattening KML's Lat,Lng points.
        flattened_speed_polygons: Surrounding  speed polygons.
        all_polygon_speeds: Speeds from all polygons.
    Returns:
        A list of vertices found at every interval of flight state.
    """
    points = iter(flatten_points)
    point1 = next(points)
    point2 = next(points)
    
    flight_state_vertices = []
    flight_state_speeds = []
    while True:
        flight_distance = get_interpolated_value(point1, flattened_speed_polygons, all_polygon_speeds, round_value=True)
        flight_state_speeds.append(flight_distance)
        input_coord_gap = get_distance_between_two_points(point1, point2)
        if input_coord_gap <= 0:
            # points are overlapping
            point1 = point2
            point2 = next(points, None)
            if not point2:
                break
            continue
        if input_coord_gap == flight_distance:
            flight_state_vertices.append(point2)
            point1 = point2
            point2 = next(points, None)
            if not point2:
                break
        if flight_distance < input_coord_gap:
            remaining_flight_distance = input_coord_gap
            while remaining_flight_distance > flight_distance:
                state_vertex = get_vertex_between_points(point1, point2, flight_distance)
                if state_vertex:
                    state_vertex = state_vertex.coords[:][0]
                    # TODO: move it to unit tests.
                    check_if_vertex_is_correct(point1, point2, state_vertex, flight_distance)
                    flight_state_vertices.append(state_vertex)
                    point1 = state_vertex
                    remaining_flight_distance -= flight_distance
            if remaining_flight_distance > 0:
                input_coord_gap = remaining_flight_distance
        if flight_distance > input_coord_gap:
            remaining_flight_distance = flight_distance - input_coord_gap
            point1 = point2
            point2 = next(points, None)
            if not point2:
                flight_state_vertices.append(point1)
                break
            state_vertex = get_vertex_between_points(point1, point2, remaining_flight_distance)
            if state_vertex:
                state_vertex = state_vertex.coords[:][0]
                flight_state_vertices.append(state_vertex)
                if state_vertex == point2:  # This is the special case when remaining_distance is very close to flight_distance
                    point2 = next(points, None)
                    if not point2:
                        flight_state_vertices.append(point1)
                        break
                point1 = state_vertex
        
    return flight_state_vertices, flight_state_speeds


def get_vertex_between_points(point1, point2, at_distance):
    """Returns vertex between point1 and point2 at a distance from point1.
    Args:
        point1: First vertex having tuple (x,y) co-ordinates.
        point2: Second vertex having tuple (x,y) co-ordinates.
        at_distance: A distance at which to locate the vertex on the line joining point1 and point2.
    Returns:
        A Point object.
    """
    line = LineString([point1, point2])
    new_point = line.interpolate(at_distance)
    return new_point

def output_coordinates_to_file(flight_state_coords, filename):
    """Writes output of state coordinates to a file.
    Args:
        flight_state_coords: Unflatten Flight state coordinates.
        filename: Output file name.
    """
    flight_state_vertices_str = '\n'.join(flight_state_coords)
    with open(f'monitoring/rid_qualifier/test_data/{filename}.txt', 'w') as text_file:
        text_file.write(flight_state_vertices_str)

def generate_flight_record(
    state_coordinates, flight_description, operator_location, flight_state_speeds):
    timestamp = datetime.now()
    now_isoformat = timestamp.isoformat()

    flight_telemetry: List[List[RIDAircraftState]] = []
    for coordinates, speed in zip(state_coordinates, flight_state_speeds):
        timestamp = timestamp + timedelta(0, STATE_INCREMENT_SECONDS)
        timestamp_isoformat = timestamp.isoformat()
        aircraft_position = RIDAircraftPosition(
            lng=coordinates[0],
            lat=coordinates[1],
            alt=coordinates[2],
            accuracy_h=flight_description.get('accuracy_h'),
            accuracy_v=flight_description.get('accuracy_v'),
            # TODO: need to check value for extrapolated
            extrapolated=False,
            )
        # TODO: RIDHeight values ?
        aircraft_height = RIDHeight(distance=ALTITUDE_AGL, reference="TakeoffLocation")
        rid_aircraft_state = RIDAircraftState(
            timestamp=timestamp_isoformat,
            # TODO: operational_status ?
            operational_status="Airborne",
            position=aircraft_position,
            height=aircraft_height,
            # TODO: track ?
            track=99999,
            # TODO: Speed to be fetched relative to speed polygon.
            speed=speed,
            timestamp_accuracy=float(flight_description.get('timestamp_accuracy', '0.0')),
            speed_accuracy=flight_description.get('speed_accuracy', ''),
            # TODO: vertical_speed ?
            vertical_speed=0.0)
        flight_telemetry.append(rid_aircraft_state)
    rid_details = RIDFlightDetails(
        id=flight_description.get('id', str(uuid.uuid4())),
        serial_number=flight_description.get('serial_number'),
        operation_description=flight_description.get('operation_description'),
        operator_location=LatLngPoint(
            lat=float(operator_location.get('lat')),
            lng=float(operator_location.get('lng'))),
        operator_id=flight_description.get('operator_id'),
        registration_number=flight_description.get('registration_number'))

    flight_details = FlightDetails(
        rid_details=rid_details,
        aircraft_type=flight_description.get('aircraft_type'),
        operator_name=flight_description.get('operator_name'))
    return FullFlightRecord(
        reference_time=now_isoformat,
        states=flight_telemetry,
        flight_details=flight_details)


def get_flight_polygons_flattened(reference_point, alt_polygons):
    """Returns flattened altitude polygons."""
    alt_polygons_flatten = []
    for input_coordinates in alt_polygons.values():
        flatten_coordinates = []
        for coord in input_coordinates:
            flatten_coordinates.append(flatten(
                s2sphere.LatLng.from_degrees(*reference_point[:2]),
                s2sphere.LatLng.from_degrees(coord[1], coord[0])
            ))
        alt_polygons_flatten.append(flatten_coordinates)

    return alt_polygons_flatten


def get_polygons_distances_from_point(point, polygons):
    """Returns a list of distances for a point from surrounding polygons.
    Args:
        point: A tuple of x,y coordinates.
        polygons: A list of flattened polygons.
    Returns:
        A list of distances in meters.
    """
    distances = []
    for poly in polygons:
        distances.append(Point(*point).distance(Polygon(poly)))
    return distances

def get_interpolated_value(point, polygons, all_possible_values, round_value=False):
    """Returns interpolated altitude wrt. to the relative distances from surrounding polygons.
    Args:
        point: A tuple of flattened x,y point.
        polygons: A list of flattened polygons.
        all_possible_values: All surrounding polygons' values. Values can be altitude or speed.
    Returns:
        An altitude number.
    """
    distances = get_polygons_distances_from_point(point, polygons)
    if min(distances) < 0.1:
        # less than 1 meter, consider it almost on the polygon.
        interpolated_value = all_possible_values[distances.index(min(distances))]
    else:
        dividend = 0
        divisor = 0
        for value, distance in zip(all_possible_values, distances):
            dividend += value/distance
            divisor += 1/distance 
        interpolated_value = dividend / divisor
    return round(interpolated_value, 2) if round_value else interpolated_value


def get_speeds_from_speed_polygons(speed_polygons):
    return [kml.get_polygon_speed(n) for n in list(speed_polygons)]
   

def get_flight_state_coordinates(flight_details):
    """Returns flight's state coordinates at speed/sec.
    Args:
        flight_details: Flight details from the KML that include input coordinates.
    Returns:
        List of unflatten coordinates at a speed/sec interval and list of speeds at each point.
    """
    if flight_details.get('input_coordinates'):
        input_coordinates = get_flight_coordinates(flight_details['input_coordinates'])
    reference_point = input_coordinates[0] # TODO: Check `if input_coordinates:`.
    
    flatten_points = []
    for point in input_coordinates:
        flatten_points.append(flatten(
            s2sphere.LatLng.from_degrees(*reference_point[:2]),
            s2sphere.LatLng.from_degrees(*point[:2])
        ))
    
    speed_polygons = flight_details['speed_polygons']
    flattened_speed_polygons = get_flight_polygons_flattened(reference_point, speed_polygons)
    all_polygon_speeds = get_speeds_from_speed_polygons(speed_polygons)
    flight_state_vertices, flight_state_speeds = get_flight_state_vertices(
        flatten_points, flattened_speed_polygons, all_polygon_speeds)
    
    alt_polygons = flight_details['alt_polygons']
    flattened_alt_polygons = get_flight_polygons_flattened(reference_point, alt_polygons)
    all_polygon_alts = [p[0][2] for p in list(alt_polygons.values())]

    # return
    
    flight_state_altitudes = []
    # flight_state_speeds = []
    for point in flight_state_vertices:
        flight_state_altitudes.append(
            get_interpolated_value(point, flattened_alt_polygons, all_polygon_alts))
        # flight_state_speeds.append(
        #     get_interpolated_value(point, flattened_speed_polygons, all_polygon_speeds, round_value=True))
    flight_state_vertices_unflatten = [unflatten(
        s2sphere.LatLng.from_degrees(*reference_point[:2]), v) for v in flight_state_vertices]

    # Position Lat, Lng to Lng, Lat order for KML representation.
    flight_state_coordinates = []
    for p, alt in zip(flight_state_vertices_unflatten, flight_state_altitudes):
        flight_state_coordinates.append((
            p.lng().degrees, p.lat().degrees, alt
        ))
    return flight_state_coordinates, flight_state_speeds


def write_to_json_file(data, file_name, output_folder):
    with open(f'{output_folder}/{file_name}.json', 'w') as outfile:
        outfile.write(json.dumps(data))


def create_output_folder(folder_path):
    if not os.path.isdir(folder_path):
        os.makedirs(folder_path)

def main(kml_file, debug_mode=None):
    # kml_file = 'monitoring/rid_qualifier/test_data/dcdemo.kml'
    kml_content = kml.get_kml_content(kml_file)
    flight_state_coordinates = {}
    output_folder = 'monitoring/rid_qualifier/test_data'
    create_output_folder(output_folder)
    for flight_name, flight_details in kml_content.items():
        flight_description = flight_details['description']
        operator_location = flight_details['operator_location']
        flight_state_coordinates, flight_state_speeds = get_flight_state_coordinates(
            flight_details)
        if debug_mode:
            flight_state_vertices_unflatten = [','.join(p) for p in flight_state_coordinates]
            flight_state_vertices_str = '\n'.join(flight_state_vertices_unflatten)
            with open(f'{output_folder}/kml_state_{flight_name}.txt', 'w') as text_file:
                text_file.write(flight_state_vertices_str)
        flight_record = generate_flight_record(
            flight_state_coordinates,
            flight_description,
            operator_location,
            flight_state_speeds)
        write_to_json_file(flight_record, flight_name.replace('flight: ', ''), output_folder=output_folder)

def init_argparse() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTION] [FILE]",
        description="Generates All Flights' state records from the KML file."
    )
    parser.add_argument(
        "-f", "--kml-file",
        help='Path to flight record KML file',
        type=str, default=None, required=True
    )
    parser.add_argument(
        "-d", "--debug",
        help='Set Debug to true to generate output coordinates to test in KML.',
        type=bool, default=None)
    return parser


if __name__ == '__main__':
    parser = init_argparse()
    args = parser.parse_args()
    if args.kml_file:
        kml_file = args.kml_file
        print(kml_file)
        if os.path.isfile(kml_file):
            file = open(kml_file, 'r')
        else:
            raise 'Invalid file path.'
        debug_mode = args.debug
        main(kml_file, debug_mode=debug_mode)
