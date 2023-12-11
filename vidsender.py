import os
import json
import time
import subprocess
from unidecode import unidecode
from ffmpy import FFmpeg, FFprobe
from pyrogram import Client, errors
from pathlib import Path
from natsort import natsorted
from typing import Tuple, Union
from utils import *
from modules.channel_description import generate_description
from modules.summary_generator import *
from auto_zip import prepare_files_for_upload
from modules.vidconverter.video_converter import convert_videos_in_folder
from modules.video_splitter import split_videos
from concurrent.futures import ThreadPoolExecutor

threads = 4
thumbnail_path = Path('templates/thumb.jpg')

def clean_console():
    os.system('clear || cls')

def progress(current, total, video_number, total_videos):
    os.system('clear || cls')
    print(f"Uploading video {video_number}/{total_videos}")
    print(f"{current * 100 / total:.1f}%")

def update_channel_info(client: Client, ch_desc: str, ch_tile: str) -> Tuple[int, str, str]:
    dest_id = client.create_channel(ch_tile).id    
    invite_link = client.export_chat_invite_link(dest_id)
    ch_desc = ch_desc + f'\nConvite: {invite_link}'
    time.sleep(10)
    client.set_chat_description(dest_id, ch_desc)

    try:
        client.set_chat_protected_content(dest_id, True)
    except errors.ChatNotModified:
        pass
    return dest_id, ch_desc, invite_link

class VideoUploader:
    def __init__(self, folder_path: str, chat_id: Union[str, int] = None, upload_status=None) -> None:
        self.folder_path = folder_path
        self.chat_id = chat_id
        self.upload_status = upload_status if upload_status else self.read_upload_status(folder_path)

    @staticmethod
    def read_upload_status(folder_path):
        json_filename = f"{Path(folder_path).stem}_upload_plan.json"
        try:
            with open(Path('projects') / json_filename, 'r', encoding='utf-8') as file:
                return json.load(file)
        except FileNotFoundError:
            return {"channel_id": None, "videos": {}}

    def write_upload_status(self):
        json_filename = f"{Path(self.folder_path).stem}_upload_plan.json"
        os.makedirs('projects', exist_ok=True)
        with open(Path('projects') / json_filename, 'w', encoding='utf-8') as file:
            json.dump(self.upload_status, file, ensure_ascii=False, indent=4)

    def update_video_status(self, video_path, status):
        self.upload_status["videos"][video_path]["status"] = status
        self.write_upload_status()

    def init_session(self, session_name: str = "user") -> None:
        try:
            self.client = Client(session_name)
            self.client.start()
        except (AttributeError, ConnectionError):
            phone_number = input("\nEnter your phone number: ")
            api_id = int(input("Enter your API ID: "))
            api_hash = input("Enter your API hash: ")

            self.client = Client(
                session_name=session_name,
                api_id=api_id,
                api_hash=api_hash.strip(),
                phone_number=phone_number.strip()
            )
            self.client.start()

    def collect_video_metadata(self, video_path: str) -> dict:
        if Path(video_path).suffix.lower() != ".mp4":
            return {}

        ffprobe_cmd = FFprobe(
            inputs={video_path: None},
            global_options=['-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams']
        )
        result = ffprobe_cmd.run(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        output_str = result[0].decode('utf-8').strip()
        metadata = json.loads(output_str)
        return metadata

    def format_summary_from_template(self, summary: str) -> str:
        template_path = Path('templates/summary_template.txt')
        with open(template_path, 'r', encoding='utf-8') as template_file:
            template = template_file.read()
            return template.format(summary_content=summary)   

    def upload_files(self):
        if not self.chat_id:
            if self.upload_status["channel_id"]:
                self.ch_id = self.upload_status["channel_id"]
            else:
                title = Path(self.folder_path).name
                self.ch_id, ch_desc, invite_link = update_channel_info(self.client, generate_description(self.folder_path), title)
                self.upload_status["channel_id"] = self.ch_id
                self.write_upload_status()
        else:
            self.ch_id = int(self.chat_id) if self.chat_id.lstrip('-').isdigit() else self.chat_id

        # Preparar uma lista de todos os arquivos de vídeo e seus índices do plano de upload
        all_videos = [(video_path, details['index']) for video_path, details in self.upload_status['videos'].items() if Path(video_path).is_file()]
        
        # Ordenar a lista pelo índice (segundo elemento de cada tupla)
        sorted_videos = sorted(all_videos, key=lambda x: x[1])

        total_videos = len(sorted_videos)

        for video_path, index in sorted_videos:
            if self.upload_status["videos"][video_path]["status"] == 1:
                continue  # Se o vídeo já foi enviado, pule

            metadata = self.collect_video_metadata(video_path)
            
            if 'streams' in metadata:
                video_stream = next((stream for stream in metadata['streams'] if stream['codec_type'] == 'video'), None)
                if video_stream:
                    width = int(video_stream['width'])
                    height = int(video_stream['height'])
                    duration = int(float(video_stream['duration']))
                else:
                    width, height, duration = None, None, None
            else:
                continue  # Skip this iteration if 'streams' key is not in metadata
            
            thumbnail_path = Path('templates/thumb.jpg')
            with open(thumbnail_path, 'rb') as thumb, open(Path(video_path), "rb") as video_file:
                caption = f"#F{index:02} {Path(video_path).name}"

                self.client.send_video(
                    self.ch_id,
                    video_file,
                    width=width,
                    height=height,
                    duration=duration,
                    caption=caption,
                    progress=progress,
                    progress_args=(index, total_videos,),
                    thumb=thumb
                )

            self.update_video_status(video_path, 1)


    def upload_zip_files(self):
        zip_folder = Path("zip_files")

        if not zip_folder.exists():
            print(f"Pasta {zip_folder} não encontrada!")
            return

        zip_files = sorted([f for f in zip_folder.rglob('*') if f.is_file() and f.suffix == '.zip'], key=lambda f: f.stem)

        if not zip_files:
            print("Nenhum arquivo .zip encontrado na pasta zip_files!")
            return

        print(f"{len(zip_files)} arquivos .zip encontrados. Iniciando o upload...")

        for index, zip_file in enumerate(zip_files, start=1):
            caption = f"#M{index:02} {zip_file.name}"
            try:
                def progress_wrapper(current, total, file_index=index, total_files=len(zip_files)):
                    progress(current, total, file_index, total_files)

                self.client.send_document(
                    self.ch_id,
                    zip_file,
                    caption=caption,
                    progress=progress_wrapper,
                )

            except Exception as e:
                print(f"Erro ao enviar {zip_file.name}. Erro: {str(e)}")

            summary = generate_summary(self.folder_path)
            formatted_summary = self.format_summary_from_template(summary)

            max_length = 4000
            if len(formatted_summary) > max_length:
                summaries = split_summary(formatted_summary, max_length)
                for idx, s in enumerate(summaries):
                    sent_msg = self.client.send_message(self.ch_id, s)
                    if idx == 0:
                        self.client.pin_chat_message(self.ch_id, sent_msg.id)
            else:
                sent_msg = self.client.send_message(self.ch_id, formatted_summary)
                self.client.pin_chat_message(self.ch_id, sent_msg.id)

def create_upload_plan(folder_path: str):
    json_filename = f"{Path(folder_path).stem}_upload_plan.json"
    upload_plan_path = Path('projects') / json_filename

    if not upload_plan_path.exists():
        video_paths = [str(video_path) for video_path in Path(folder_path).rglob('*.mp4')]            
        normalized_paths = [unidecode(path) for path in video_paths]            
        sorted_paths = natsorted(normalized_paths)
        videos = {video_path: {"status": 0, "index": i + 1} for i, video_path in enumerate(sorted_paths)}
        upload_status = {"channel_id": None, "videos": videos}
        os.makedirs('projects', exist_ok=True)
        with open(upload_plan_path, 'w', encoding='utf-8') as file:
            json.dump(upload_status, file, ensure_ascii=False, indent=4)
        return upload_status
    else:
        return VideoUploader.read_upload_status(folder_path)                

def main():
    clean_console()                        
    show_banner()
    authenticate()
    folder_path = input("Informe o caminho da pasta que deseja fazer o upload: ")
    upload_status = VideoUploader.read_upload_status(folder_path)
    if upload_status["videos"]:
        print("Plano de upload encontrado. Iniciando o upload diretamente...")
    else:
        #clear_directory('zip_files')
        normalize_filenames(folder_path)
        generate_report(folder_path)
        delete_residual_files(folder_path)
        clean_console()
        convert_videos_in_folder(folder_path)
        prepare_files_for_upload(folder_path, threads)
        split_videos(folder_path, size_limit="2 GB", delete_corrupted_video=True)
        upload_status = create_upload_plan(folder_path)

    uploader = VideoUploader(folder_path, upload_status=upload_status)
    uploader.init_session()
    uploader.upload_files()
    uploader.upload_zip_files()

if __name__ == "__main__":
    main()