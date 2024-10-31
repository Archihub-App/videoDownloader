from app.utils.PluginClass import PluginClass
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.utils import DatabaseHandler
from app.utils import HookHandler
from flask import request
from celery import shared_task
from dotenv import load_dotenv
import os
from app.api.users.services import has_role
from app.api.records.models import RecordUpdate
from bson.objectid import ObjectId
import ffmpeg
# from .utils.patch import CustomCipher
from pytubefix import YouTube
# from pytube import cipher
import json
from datetime import datetime
import time
import re
# cipher.Cipher = CustomCipher
load_dotenv()

mongodb = DatabaseHandler.DatabaseHandler()
hookHandler = HookHandler.HookHandler()

USER_FILES_PATH = os.environ.get('USER_FILES_PATH', '')
WEB_FILES_PATH = os.environ.get('WEB_FILES_PATH', '')
ORIGINAL_FILES_PATH = os.environ.get('ORIGINAL_FILES_PATH', '')
TEMPORAL_FILES_PATH = os.environ.get('TEMPORAL_FILES_PATH', '')

class ExtendedPluginClass(PluginClass):
    def __init__(self, path, import_name, name, description, version, author, type, settings):
        super().__init__(path, __file__, import_name, name, description, version, author, type, settings)

    def add_routes(self):
        @self.route('/bulk', methods=['POST'])
        @jwt_required()
        def process_files():
            current_user = get_jwt_identity()
            body = request.get_json()

            if 'post_type' not in body:
                return {'msg': 'No se especificó el tipo de contenido'}, 400
            
            if 'parent' not in body or not body['parent']:
                return {'msg': 'No se especificó el padre del contenido'}, 400
            
            if 'url' not in body:
                return {'msg': 'No se especificó la URL del video'}, 400
            
            if not self.has_role('admin', current_user) and not self.has_role('processing', current_user):
                return {'msg': 'No tiene permisos suficientes'}, 401
            
            task = self.bulk.delay(body, current_user)
            self.add_task_to_user(task.id, 'videoDownloader.download', current_user, 'msg')
            
            return {'msg': 'Se agregó la tarea a la fila de procesamientos'}, 201
        
    def get_settings(self):
        @self.route('/settings/<type>', methods=['GET'])
        @jwt_required()
        def get_settings(type):
            try:
                current_user = get_jwt_identity()

                if not has_role(current_user, 'admin') and not has_role(current_user, 'processing'):
                    return {'msg': 'No tiene permisos suficientes'}, 401
                
                if type == 'all':
                    return self.settings
                elif type == 'settings':
                    return self.settings['settings']
                elif type == 'bulk':
                    from app.api.system.services import get_resources_schema
                    schema = get_resources_schema()
                    metadata = schema['schema']['metadata']

                    metadata_paths = []
                    
                    def get_paths(data, path, tipo):
                        for key in data:
                            if isinstance(data[key], dict):
                                get_paths(data[key], path + key + '.', tipo)
                            elif 'type' in data:
                                if data['type'] in tipo:
                                    path = path[:-1]
                                    metadata_paths.append(path)
                    
                    get_paths(metadata, 'metadata.', ['text'])
                    metadata_paths = metadata_paths[::-1]

                    new_settings = [*self.settings['settings_' + type]]
                    new_settings.append({
                        'type': 'select',
                        'id': 'metadata_title',
                        'label': 'Campo de titulo',
                        'default': 'metadata.firstLevel.title',
                        'options': [{'value': t, 'label': t} for t in metadata_paths],
                        'required': True
                    })

                    new_settings.append({
                        'type': 'select',
                        'id': 'metadata_author',
                        'label': 'Campo de autor',
                        'default': '',
                        'options': [{'value': t, 'label': t} for t in metadata_paths],
                    })

                    metadata_paths = []
                    get_paths(metadata, 'metadata.', ['text-area'])
                    metadata_paths = metadata_paths[::-1]

                    new_settings.append({
                        'type': 'select',
                        'id': 'metadata_description',
                        'label': 'Campo de descripción',
                        'default': '',
                        'options': [{'value': t, 'label': t} for t in metadata_paths],
                    })

                    metadata_paths = []
                    get_paths(metadata, 'metadata.', ['simple-date'])
                    metadata_paths = metadata_paths[::-1]

                    new_settings.append({
                        'type': 'select',
                        'id': 'metadata_publish_date',
                        'label': 'Campo de fecha de publicación',
                        'default': '',
                        'options': [{'value': t, 'label': t} for t in metadata_paths],
                    })
                    return new_settings
                else:
                    return self.settings['settings_' + type]
            except Exception as e:
                print(str(e))
                return {'msg': str(e)}, 500

    @shared_task(ignore_result=False, name='videoDownloader.download')
    def bulk(body, user):
        def modify_dict(d, path, value):
            keys = path.split('.')
            for key in keys[:-1]:
                d = d.setdefault(key, {})
            d[keys[-1]] = value

        url = body['url'].split(',')

        for u in url:
            attempt = 0
            max_attempts = 3
            while attempt < max_attempts:
                try:
                    yt = YouTube(u)
                    while yt.streams is None:
                        yt = YouTube(u)
                    downloaded_file_path = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first().download(TEMPORAL_FILES_PATH)

                    # obtener ruta del archivo
                    filename = os.path.basename(downloaded_file_path)
                    path = os.path.join(TEMPORAL_FILES_PATH, filename)
                    # obtener la descripción del video
                    description = yt.description
                    # obtener la fecha de publicación del video
                    publish_date = yt.publish_date

                    if body['extract_audio']:
                        # Extraer audio del video
                        audio_path = os.path.join(TEMPORAL_FILES_PATH, filename.split('.')[0] + '.mp3')
                        ffmpeg.input(downloaded_file_path).output(audio_path).run()
                        # Eliminar archivo original
                        os.remove(downloaded_file_path)
                        downloaded_file_path = audio_path
                        filename = os.path.basename(downloaded_file_path)
                        path = os.path.join(TEMPORAL_FILES_PATH, filename)

                    data = {}
                    modify_dict(data, 'metadata.firstLevel.title', yt.title)
                    if body['metadata_description'] != '':
                        modify_dict(data, body['metadata_description'], description)
                    if body['metadata_publish_date'] != '':
                        modify_dict(data, body['metadata_publish_date'], publish_date)
                    if body['metadata_author'] != '':
                        modify_dict(data, body['metadata_author'], yt.author)

                    data['post_type'] = body['post_type']
                    data['parent'] = [{'id': body['parent']}]
                    data['parents'] = [{'id': body['parent']}]
                    data['status'] = 'published'
                    data['createdBy'] = user
                    data['filesIds'] = [{
                        'file': 0,
                        'filetag': 'file',
                    }]

                    from app.api.resources.services import create as create_resource
                    create_resource(data, user, [{'file': path, 'filename': filename}])

                    # Eliminar archivo temporal
                    os.remove(downloaded_file_path)
                    break
                
                except Exception as e:
                    print(str(e))
                    time.sleep(2)
                    attempt += 1
            else:
                raise Exception('No se pudo descargar el video ' + u)
            
        return 'Descarga de videos finalizada'
                
            
    
plugin_info = {
    'name': 'Descarga de videos',
    'description': 'Plugin para descargar videos y generar versiones para consulta en el gestor documental.',
    'version': '0.1',
    'author': 'Néstor Andrés Peña',
    'type': ['bulk'],
    'settings': {
        'settings': [

        ],
        'settings_bulk': [
            {
                'type': 'instructions',
                'title': 'Instrucciones',
                'text': 'Este plugin permite descargar videos de diferentes fuentes y generar versiones para consulta en el gestor documental.'
            },
            {
                'type': 'file',
                'name': 'file',
                'label': 'Archivo Excel',
                'required': True,
                'limit': 1,
                'acceptedFiles': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
            },
            {
                'type': 'text',
                'id': 'url',
                'label': 'URL del video',
                'placeholder': 'URL del video',
                'required': True
            },
            {
                'type': 'checkbox',
                'id': 'extract_audio',
                'label': 'Solo guardar el audio',
                'default': False,
                'required': False
            }
        ]
    }
}