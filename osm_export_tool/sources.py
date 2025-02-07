import json
import os
import shutil
import subprocess
import tempfile
from xml.dom import ValidationErr
import requests
from requests.exceptions import Timeout
from string import Template
from osm_export_tool.sql import to_prefix
from urllib.parse import urlparse
import shapely.geometry

# path must return a path to an .osm.pbf or .osm.xml on the filesystem

class Pbf:
    def __init__(self,path):
        self._path = path

    def fetch(self):
        pass

    def path(self):
        return self._path

class OsmExpress:
    def __init__(self,osmx_path,db_path,geom,output_path,use_existing=True,tempdir=None):
        self.osmx_path = osmx_path
        self.db_path = db_path
        self.geom = geom
        self.output_path = output_path
        self.use_existing = use_existing
        self.tempdir = tempdir

    def fetch(self):
        region_json = os.path.join(self.tempdir,'region.json')
        with open(region_json,'w') as f:
            f.write(json.dumps(shapely.geometry.mapping(self.geom)))
        subprocess.check_call([self.osmx_path,'extract',self.db_path,self.output_path,'--region',region_json])
        os.remove(region_json)

    def path(self):
        if os.path.isfile(self.output_path) and self.use_existing:
            return self.output_path
        else:
            self.fetch()
        return self.output_path

class OsmiumTool:
    def __init__(self,osmium_path,source_path,geom,output_path,use_existing=True,tempdir=None, mapping=None):
        self.osmium_path = osmium_path
        self.source_path = source_path
        self.geom = geom
        self.output_path = output_path
        self.use_existing = use_existing
        self.tempdir = tempdir
        self.mapping = mapping

    @classmethod
    def parts(cls, expr):
        def _parts(prefix):
            op = prefix[0]
            if op == '=':
                return ["{0}={1}".format(prefix[1],prefix[2])]
            if op == '!=':
                return ["{0}!={1}".format(prefix[1],prefix[2])]
            if op in ['<','>','<=','>='] or op == 'notnull':
                raise ValueError('{0} where clause not supported'.format(op))
            if op == 'in':
                x = "{0}={1}".format(prefix[1],','.join(prefix[2]))
                return [x]
            if op == 'and' or op == 'or':
                return _parts(prefix[1]) + _parts(prefix[2])
        return _parts(expr)

    @staticmethod
    def get_element_filter(theme, part):
        elements = []
        if theme.points:
            elements.append("n/{0}".format(part)) # node
        if theme.lines:
            elements.append("w/{0}".format(part)) # way
        if theme.polygons:
            elements.append("r/{0}".format(part)) # relation

        return elements

    @classmethod
    def filters(cls,mapping):
        filters_set = set()
        tags = set()
        for t in mapping.themes:
            prefix = t.matcher.expr
            parts = cls.parts(prefix)
            for part in parts:
                [filters_set.add(e) for e in OsmiumTool.get_element_filter(t, part)]
                key = [t for t in t.keys if t in part]
                if len(key) == 1:
                    tags.add(key[0])

        return filters_set

    def tags_filter(self, filters, planet_as_source):
        source_path = self.output_path
        if planet_as_source is True:
            source_path = self.source_path

        cmd = [self.osmium_path,'tags-filter',source_path,'-o',self.output_path]

        for f in filters:
            cmd.insert(3, f)

        if planet_as_source is False:
            cmd.append('--overwrite')

        subprocess.check_call(cmd)

    def fetch(self):
        region_json = os.path.join(self.tempdir,'region.json')
        with open(region_json,'w') as f:
            f.write(json.dumps({'type':'Feature','geometry':shapely.geometry.mapping(self.geom)}))
        subprocess.check_call([self.osmium_path,'extract','-p',region_json,self.source_path,'-o',self.output_path,'--overwrite'])
        os.remove(region_json)

    def path(self):
        if os.path.isfile(self.output_path) and self.use_existing:
            return self.output_path

        planet_as_source = True
        if self.geom.area < 6e4:
            self.fetch()
            planet_as_source = False

        if self.mapping is not None:
            filters = OsmiumTool.filters(self.mapping)
            self.tags_filter(filters, planet_as_source)

        return self.output_path


class Overpass:
    @classmethod
    def filters(cls,mapping):
        nodes = set()
        ways = set()
        relations = set()
        for t in mapping.themes:
            parts = cls.parts(t.matcher.expr)
            if t.points:
                for part in parts:
                    nodes.add(part)
            if t.lines:
                for part in parts:
                    ways.add(part)
            if t.polygons:
                for part in parts:
                    ways.add(part)
                    relations.add(part)
        return nodes,ways,relations

    # force quoting of strings to handle keys with colons
    @classmethod
    def parts(cls, expr):
        def _parts(prefix):
            op = prefix[0]
            if op == '=':
                return ["['{0}'='{1}']".format(prefix[1],prefix[2])]
            if op == '!=':
                return ["['{0}'!='{1}']".format(prefix[1],prefix[2])]
            if op in ['<','>','<=','>='] or op == 'notnull':
                return ["['{0}']".format(prefix[1])]
            if op == 'in':
                x = "['{0}'~'{1}']".format(prefix[1],'|'.join(prefix[2]))
                return [x]
            if op == 'and' or op == 'or':
                return _parts(prefix[1]) + _parts(prefix[2])
        return _parts(expr)

    @classmethod
    def sql(cls,str):
        return cls.parts(to_prefix(str))

    def __init__(self,hostname,geom,path,use_existing=True,tempdir=None,osmconvert_path='osmconvert',mapping=None,use_curl=False):
        self.hostname = hostname
        self._path = path
        self.geom = geom
        self.use_existing = use_existing
        self.osmconvert_path = osmconvert_path
        self.tmp_path = os.path.join(tempdir,'tmp.osm.xml')
        self.mapping = mapping
        self.use_curl = use_curl
        self.tempdir = tempdir

    def fetch(self):
        base_template = Template('[maxsize:$maxsize][timeout:$timeout];$query;out meta;')

        if self.geom.geom_type == 'Polygon':
            geom = 'poly:"{0}"'.format(' '.join(['{1} {0}'.format(*x) for x in self.geom.exterior.coords]))
        else:
            bounds = self.geom.bounds
            west = max(bounds[0], -180)
            south = max(bounds[1], -90)
            east = min(bounds[2], 180)
            north = min(bounds[3], 90)
            geom = '{1},{0},{3},{2}'.format(west, south, east, north)

        if self.mapping:
            query = """(
                (
                    {0}
                );
                (
                    {1}
                );>;
                (
                    {2}
                );>>;>;)"""
            nodes,ways,relations = Overpass.filters(self.mapping)
            nodes = '\n'.join(['node({0}){1};'.format(geom,f) for f in nodes])
            ways = '\n'.join(['way({0}){1};'.format(geom,f) for f in ways])
            relations = '\n'.join(['relation({0}){1};'.format(geom,f) for f in relations])
            query = query.format(nodes,ways,relations)
        else:
            query = '(node({0});<;>>;>;)'.format(geom)

        data = base_template.substitute(maxsize=2147483648,timeout=1600,query=query)

        if self.use_curl:
            with open(os.path.join(self.tempdir,'query.txt'),'w') as query_txt:
                query_txt.write(data)
            subprocess.check_call(['curl','-X','POST','-d','@'+os.path.join(self.tempdir,'query.txt'),os.path.join(self.hostname,'api','interpreter'),'-o',self.tmp_path])
        else:
            with requests.post(os.path.join(self.hostname,'api','interpreter'),data=data, stream=True) as r:
                
                with open(self.tmp_path, 'wb') as f:
                    shutil.copyfileobj(r.raw, f)

        with open(self.tmp_path,'r') as f:
            sample = [next(f) for x in range(6)]
            if 'DOCTYPE html' in sample[1]:
                raise Exception('Overpass failure')
            if 'remark' in sample[5]:
                raise Exception(sample[5])
        # run osmconvert on the file
        try:
            subprocess.check_call([self.osmconvert_path,self.tmp_path,'--out-pbf','-o='+self._path])
        except subprocess.CalledProcessError as e:
            raise ValidationErr(e)
        os.remove(self.tmp_path)

    def path(self):
        if os.path.isfile(self._path) and self.use_existing:
            return self._path
        else:
            self.fetch()
        return self._path


class Galaxy:
    """Transfers Yaml Language to Galaxy Query Make a request and sends response back from fetch()"""
    
    @classmethod
    def hdx_filters(cls,t):
        geometryType=[]
        point_filter,line_filter,poly_filter={},{},{}
        point_columns,line_columns,poly_columns=[],[],[]
        parts = cls.parts(t.matcher.expr)
        if t.points:
            point_columns=cls.attribute_filter(t)
            geometryType.append("point")
            for part in parts:
                part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                for key,value in part_dict.items():
                    if key not in point_filter:
                        point_filter[key] = value
                    else:
                        point_filter[key] += value # dictionary already have that key defined update and add the value
        if t.lines:
            ways_select_filter=cls.attribute_filter(t)
            line_columns=cls.attribute_filter(t)

            geometryType.append("line") # Galaxy supports both linestring and multilinestring, getting them both since export tool only has line but with galaxy it will also deliver multilinestring features 
            for part in parts:
                part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                for key,value in part_dict.items():
                    if key not in line_filter:
                        line_filter[key] = value
                    else:
                        line_filter[key] += value
        if t.polygons:
            poly_columns=cls.attribute_filter(t)
            geometryType.append("polygon" ) # Galaxy also supports multipolygon and polygon , passing them both since export tool has only polygon supported
            for part in parts:
                part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                for key,value in part_dict.items():
                    if key not in poly_filter:
                        poly_filter[key] = value
                    else:
                        if poly_filter.get(key) != []: #only add other values if not null condition is not applied to that key
                            if value == [] : # if incoming value is not null i.e. key = * ignore previously added values
                                poly_filter[key] = value
                            else:
                                poly_filter[key] += value # if value was not previously = * then and value is not =* then add values 

        if point_filter:
            point_filter=cls.remove_duplicates(point_filter)
        if line_filter:
            line_filter=cls.remove_duplicates(line_filter)
        if poly_filter:
            poly_filter=cls.remove_duplicates(poly_filter)
        return point_filter,line_filter,poly_filter,geometryType,point_columns,line_columns,poly_columns

    
    @classmethod
    def filters(cls,mapping):
        geometryType=[]
        point_filter,line_filter,poly_filter={},{},{}
        point_columns,line_columns,poly_columns=[],[],[]
        
        for t in mapping.themes:
            
            parts = cls.parts(t.matcher.expr)
            if t.points:
                point_columns=cls.attribute_filter(t)
                geometryType.append("point")
                for part in parts:
                    part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                    for key,value in part_dict.items():
                        if key not in point_filter:
                            point_filter[key] = value
                        else:
                            point_filter[key] += value # dictionary already have that key defined update and add the value
            if t.lines:
                ways_select_filter=cls.attribute_filter(t)
                line_columns=cls.attribute_filter(t)

                geometryType.append("line") # Galaxy supports both linestring and multilinestring, getting them both since export tool only has line but with galaxy it will also deliver multilinestring features 
                for part in parts:
                    part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                    for key,value in part_dict.items():
                        if key not in line_filter:
                            line_filter[key] = value
                        else:
                            line_filter[key] += value
            if t.polygons:
                poly_columns=cls.attribute_filter(t)
                geometryType.append("polygon" ) # Galaxy also supports multipolygon and polygon , passing them both since export tool has only polygon supported
                for part in parts:
                    part_dict=json.loads(f"""{'{'}{part.strip()}{'}'}""")
                    for key,value in part_dict.items():
                        if key not in poly_filter:
                            poly_filter[key] = value
                        else:
                            if poly_filter.get(key) != []: #only add other values if not null condition is not applied to that key
                                if value == [] : # if incoming value is not null i.e. key = * ignore previously added values
                                    poly_filter[key] = value
                                else:
                                    poly_filter[key] += value # if value was not previously = * then and value is not =* then add values 

        if point_filter:
            point_filter=cls.remove_duplicates(point_filter)
        if line_filter:
            line_filter=cls.remove_duplicates(line_filter)
        if poly_filter:
            poly_filter=cls.remove_duplicates(poly_filter)
        return point_filter,line_filter,poly_filter,geometryType,point_columns,line_columns,poly_columns

    @classmethod
    def remove_duplicates(cls,entries_dict):
        for key,value in entries_dict.items():
            entries_dict[key]=list(dict.fromkeys(value))
        return entries_dict

    # force quoting of strings to handle keys with colons
    @classmethod
    def parts(cls, expr):
        def _parts(prefix):
            op = prefix[0]
            if op == '=':
                return [""" "{0}":["{1}"] """.format(prefix[1],prefix[2])]
            if op == '!=': #fixme this will require improvement in galaxy api is not implemented yet
                pass
                # return ["['{0}'!='{1}']".format(prefix[1],prefix[2])]
            if op in ['<','>','<=','>='] or op == 'notnull':
                return [""" "{0}":[] """.format(prefix[1])]
            if op == 'in':
                x = """ "{0}":["{1}"]""".format(prefix[1],""" "," """.join(prefix[2]))
                return [x]
            if op == 'and' or op == 'or':
                return _parts(prefix[1]) + _parts(prefix[2])
        return _parts(expr)

    @classmethod
    def attribute_filter(cls, theme):
        columns = theme.keys
        return list(columns)

    def __init__(self,hostname,geom,mapping=None,file_name=""):
        self.hostname = hostname
        self.geom = geom
        self.mapping = mapping  
        self.file_name=file_name

    def fetch(self,output_format,is_hdx_export=False):
        if self.geom.geom_type == 'Polygon':
            geom=shapely.geometry.mapping(self.geom) # converting geom to geojson
        else: #fixme
            bounds = self.geom.bounds
            west = max(bounds[0], -180)
            south = max(bounds[1], -90)
            east = min(bounds[2], 180)
            north = min(bounds[3], 90)
            geom = '{1},{0},{3},{2}'.format(west, south, east, north)
          
        
        if self.mapping:
            if is_hdx_export:
                fullresponse=[]
                for t in self.mapping.themes:
                    point_filter,line_filter,poly_filter,geometryType_filter,point_columns,line_columns,poly_columns = Galaxy.hdx_filters(t)
                    osmTags=point_filter
                    if point_filter == line_filter == poly_filter : 
                        osmTags=point_filter # master filter that will be applied to all type of osm elements : current implementation of galaxy api 
                    else :
                        osmTags ={}
                    if point_columns == line_columns == poly_columns:
                        columns=point_columns
                    else :
                        columns =[]
                    if len(geometryType_filter) == 0:
                        geometryType_filter=["point","line","polygon"]
                    
                    for geomtype in geometryType_filter:
                        geomtype_to_pass=[geomtype]
                        if osmTags: # if it is a master filter i.e. filter same for all type of feature
                            if columns:
                                request_body={"fileName":f"""{self.file_name}-{t.name}-{geomtype}""","geometry":geom,"outputType":output_format,"geometryType":geomtype_to_pass,"filters":{"tags":{"all_geometry":osmTags},"attributes":{"all_geometry":columns}}}
                            else :
                                request_body={"fileName":f"""{self.file_name}-{t.name}-{geomtype}""","geometry":geom,"outputType":output_format,"geometryType":geomtype_to_pass,"osmTags":osmTags,"filters":{"tags":{"all_geometry":osmTags},"attributes":{"point":point_columns,"line":line_columns,"polygon":poly_columns}}}
                        else:
                            if columns:
                                request_body={"fileName":f"""{self.file_name}-{t.name}-{geomtype}""","geometry":geom,"outputType":output_format,"geometryType":geomtype_to_pass,"filters":{"tags":{"point":point_filter,"line":line_filter,"polygon":poly_filter},"attributes":{"all_geometry":columns}}}
                            else :
                                request_body={"fileName":f"""{self.file_name}-{t.name}-{geomtype}""","geometry":geom,"outputType":output_format,"geometryType":geomtype_to_pass,"filters":{"tags":{"point":point_filter,"line":line_filter,"polygon":poly_filter},"attributes":{"point":point_columns,"line":line_columns,"polygon":poly_columns}}}
                        # sending post request and saving response as response object
                        headers = {'accept': "application/json","Content-Type": "application/json"}
                        # print(request_body)
                        try :
                            with requests.Session() as req_session:
                                r=req_session.post(url = self.hostname, data = json.dumps(request_body) ,headers=headers,timeout=60*45)
                                r.raise_for_status()
                                if r.ok :
                                    response_back = r.json()
                                    response_back['theme'] = t.name
                                    response_back['output_name'] = output_format
                                    # print(response_back)
                                    fullresponse.append(response_back)
                                else :
                                    # print(r.content)
                                    raise ValueError(r.content)
                        except requests.exceptions.RequestException as e:
                            raise e
                            
                return fullresponse
            else:
                point_filter,line_filter,poly_filter,geometryType_filter,point_columns,line_columns,poly_columns = Galaxy.filters(self.mapping)
                osmTags=point_filter
                if point_filter == line_filter == poly_filter : 
                    osmTags=point_filter # master filter that will be applied to all type of osm elements : current implementation of galaxy api 
                else :
                    osmTags ={}
                if point_columns == line_columns == poly_columns:
                    columns=point_columns
                else :
                    columns =[]
        else:
            geometryType_filter=[] # if nothing is provided we are getting all type of data back
        
        if osmTags: # if it is a master filter i.e. filter same for all type of feature
            if columns:
                request_body={"fileName":self.file_name,"geometry":geom,"outputType":output_format,"geometryType":geometryType_filter,"filters":{"tags":{"all_geometry":osmTags},"attributes":{"all_geometry":columns}}}
            else :
                request_body={"fileName":self.file_name,"geometry":geom,"outputType":output_format,"geometryType":geometryType_filter,"osmTags":osmTags,"filters":{"tags":{"all_geometry":osmTags},"attributes":{"point":point_columns,"line":line_columns,"polygon":poly_columns}}}
        else:
            if columns:
                request_body={"fileName":self.file_name,"geometry":geom,"outputType":output_format,"geometryType":geometryType_filter,"filters":{"tags":{"point":point_filter,"line":line_filter,"polygon":poly_filter},"attributes":{"all_geometry":columns}}}
            else :
                request_body={"fileName":self.file_name,"geometry":geom,"outputType":output_format,"geometryType":geometryType_filter,"filters":{"tags":{"point":point_filter,"line":line_filter,"polygon":poly_filter},"attributes":{"point":point_columns,"line":line_columns,"polygon":poly_columns}}}
        headers = {'accept': "application/json","Content-Type": "application/json"}
        # print(request_body)
        try:
            with requests.Session() as req_session:
                r=req_session.post(url = self.hostname, data = json.dumps(request_body) ,headers=headers,timeout=60*45)
                r.raise_for_status()
                if r.ok :
                    response_back = r.json()
                    return [response_back]
                else :
                    raise ValueError(r.content)
        except requests.exceptions.RequestException as e:
            raise e


class Hootenanny:
    @classmethod
    def filters(cls,mapping):
        nodes = set()
        ways = set()
        relations = set()
        for t in mapping.themes:
            parts = cls.parts(t.matcher.expr)
            if t.points:
                for part in parts:
                    nodes.add(part)
            if t.lines:
                for part in parts:
                    ways.add(part)
            if t.polygons:
                for part in parts:
                    ways.add(part)
                    relations.add(part)
        return nodes,ways,relations

    # force quoting of strings to handle keys with colons
    @classmethod
    def parts(cls, expr):
        def _parts(prefix):
            op = prefix[0]
            if op == '=':
                return ["['{0}'='{1}']".format(prefix[1],prefix[2])]
            if op == '!=':
                return ["['{0}'!='{1}']".format(prefix[1],prefix[2])]
            if op in ['<','>','<=','>='] or op == 'notnull':
                return ["['{0}']".format(prefix[1])]
            if op == 'in':
                x = "['{0}'~'{1}']".format(prefix[1],'|'.join(prefix[2]))
                return [x]
            if op == 'and' or op == 'or':
                return _parts(prefix[1]) + _parts(prefix[2])
        return _parts(expr)

    @classmethod
    def sql(cls,str):
        return cls.parts(to_prefix(str))

    def __init__(self,hostname,geom,path,use_existing=True,mapping=None,extensions=['shp'], name=None, schemas_config = {}, maxGridSize=1.0):
        self.hostname = hostname
        self._path = path
        self.geom = geom
        self.use_existing = use_existing
        self.mapping = mapping
        self.extensions = extensions
        self.schemas_config = schemas_config
        self.name = name
        self.maxGridSize= maxGridSize

    def fetch(self):
        base_template = Template('[out:json][bbox];$query;out meta;')
        if self.geom.geom_type == 'Polygon':
            geom = ';'.join(['{0},{1}'.format(*x) for x in self.geom.exterior.coords])
        else:
            bounds = self.geom.bounds
            west = max(bounds[0], -180)
            south = max(bounds[1], -90)
            east = min(bounds[2], 180)
            north = min(bounds[3], 90)
            geom = '{1},{0},{3},{2}'.format(west, south, east, north)

        if self.mapping:
            query = """(
                (
                    {0}
                );
                (
                    {1}
                );>;
                (
                    {2}
                );>>;>;)"""
            nodes,ways,relations = Hootenanny.filters(self.mapping)
            nodes = '\n'.join(['node{0};'.format(f) for f in nodes])
            ways = '\n'.join(['way{0};'.format(f) for f in ways])
            relations = '\n'.join(['relation{0};'.format(f) for f in relations])
            query = query.format(nodes,ways,relations)
        else:
            query = '(node;<;>>;>;)'

        data = base_template.substitute(maxsize=2147483648,timeout=1600,query=query)

        # create a temporary file that holds the oql query
        tmp = tempfile.NamedTemporaryFile();
        with open(tmp.name, 'w') as f: f.write(data)

        path_str = self._path
        temp_pbf = os.path.join(path_str, '{0}.osm.pbf'.format(self.name))

        parsed_hostname = urlparse(self.hostname).hostname

        subprocess.check_call([
            'hoot', 'convert', '-D', 'overpass.api.host={0}'.format(parsed_hostname),
                               '-D', 'overpass.api.query.path={0}'.format(tmp.name),
                               '-D', 'bounds={0}'.format(geom),
                               '-D', 'reader.http.bbox.max.download.size={0}'.format(self.maxGridSize), # override api read limit
                                os.path.join(self.hostname, 'api', 'interpreter'), temp_pbf
        ])

        for ext in self.extensions:
            for schema in self.schemas_config:
                command_arr = ['hoot','convert','-D','schema.translation.script={0}'.format(self.schemas_config[schema])]
                if schema != 'OSM':
                    command_arr += ['-D', 'schema.translation.direction=toogr']
                command_arr += [temp_pbf, os.path.join(path_str, '{0}.{1}.{2}'.format(self.name, schema, ext))]
                subprocess.check_call(command_arr)

    def path(self):
        if os.path.isfile(self._path) and self.use_existing:
            return self._path
        else:
            self.fetch()
        return self._path
