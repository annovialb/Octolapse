# coding=utf-8
##################################################################################
# Octolapse - A plugin for OctoPrint used for making stabilized timelapse videos.
# Copyright (C) 2017  Brad Hochgesang
##################################################################################
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see the following:
# https://github.com/FormerLurker/Octolapse/blob/master/LICENSE
#
# You can contact the author either through the git-hub repository, or at the
# following email address: FormerLurker@pm.me
##################################################################################
from __future__ import unicode_literals
import math
import os
import shutil
import sys
import threading
from six.moves import queue
from six import string_types, iteritems
import time
import json
import copy
from zipfile import ZipFile
from csv import DictReader
# sarge was added to the additional requirements for the plugin
import datetime
from tempfile import mkdtemp
import uuid
import sarge
from PIL import Image, ImageDraw, ImageFont

import octoprint_octolapse.utility as utility
import octoprint_octolapse.script as script
from octoprint_octolapse.snapshot import SnapshotMetadata, CameraInfo
from octoprint_octolapse.settings import OctolapseSettings, CameraProfile
# create the module level logger
from octoprint_octolapse.log import LoggingConfigurator
logging_configurator = LoggingConfigurator()
logger = logging_configurator.get_logger(__name__)


def is_rendering_template_valid(template, options):
    # make sure we have all the replacements we need
    option_dict = {}
    for option in options:
        option_dict[option] = "F"  # use any valid file character, F seems ok
    try:
        filename = template.format(**option_dict)
    except KeyError as e:
        return False, "The following token is invalid: {{{0}}}".format(e.args[0])
    except IndexError as e:
        return False, "Integers as tokens are not allowed."
    except ValueError:
        return False, "A value error occurred when replacing the provided tokens."

    temp_directory = mkdtemp()
    file_path = "{0}{1}.{2}".format(temp_directory, filename, "mp4")
    # see if the filename is valid
    if not os.access(file_path, os.W_OK):
        try:
            open(file_path, 'w').close()
            os.unlink(file_path)
        except (IOError, OSError):
            return False, "The resulting filename is not a valid filename.  Most likely an invalid character was used."

    shutil.rmtree(temp_directory)

    return True, ""


def is_overlay_text_template_valid(template, options):
    # make sure we have all the replacements we need
    option_dict = {}
    for option in options:
        option_dict[option] = "F"  # use any valid file character, F seems ok
    try:
        template.format(**option_dict)
    except KeyError as e:
        return False, "The following token is invalid: {{{0}}}".format(e.args[0])
    except IndexError as e:
        return False, "Integers as tokens are not allowed."
    except ValueError:
        return False, "A value error occurred when replacing the provided tokens."

    return True, ""


def preview_overlay(rendering_profile, image=None):
    if rendering_profile.overlay_font_path is None or len(rendering_profile.overlay_font_path.strip()) == 0:
        # we don't have any overlay path, return
        return None

    overlay_text_color = rendering_profile.get_overlay_text_color()
    overlay_outline_color = rendering_profile.get_overlay_outline_color()
    overlay_outline_width = rendering_profile.overlay_outline_width
    if image is None:
        image_color = (0,0,0,255)
        if isinstance(overlay_text_color, list):
            image_color = tuple(255 - c for c in overlay_text_color)
        # Create an image with background color inverse to the text color.
        image = Image.new('RGB', (640, 480), color=image_color)

    try:
        font = ImageFont.truetype(rendering_profile.overlay_font_path, size=50)
    except IOError as e:
        logger.exception("An error occurred while opening the selected font")
        raise e

    def draw_center(i, t, overlay_text_color, dx=0, dy=0):
        """Draws the text centered in the image, offsets by (dx, dy)."""
        text_image = Image.new('RGBA', i.size, (255, 255, 255, 0))
        d = ImageDraw.Draw(text_image)
        iw, ih = i.size
        tw, th = d.textsize(t, font=font)

        d.text(xy=(iw / 2 - tw / 2 + dx, ih / 2 - th / 2 + dy), text=t,
               fill=tuple(overlay_text_color), font=font)
        return Image.alpha_composite(i.convert('RGBA'), text_image).convert('RGB')

    # copy the overlay text color list
    image_text_color = list(overlay_text_color)
    # set image text color to opaque
    image_text_color[3] = 255
    image = draw_center(image, "Preview", image_text_color, dy=-20)
    image = draw_center(image, "Click to refresh", image_text_color, dy=20)

    format_vars = {'snapshot_number': 1234,
                   'file_name': 'image.jpg',
                   'time_taken': time.time(),
                   'current_time': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                   'time_elapsed': "{}".format(datetime.timedelta(seconds=round(9001)))}
    image = TimelapseRenderJob.add_overlay(image,
                                           text_template=rendering_profile.overlay_text_template,
                                           format_vars=format_vars,
                                           font_path=rendering_profile.overlay_font_path,
                                           font_size=rendering_profile.overlay_font_size,
                                           overlay_location=rendering_profile.overlay_text_pos,
                                           overlay_text_alignment=rendering_profile.overlay_text_alignment,
                                           overlay_text_valign=rendering_profile.overlay_text_valign,
                                           overlay_text_halign=rendering_profile.overlay_text_halign,
                                           text_color=overlay_text_color,
                                           outline_color=overlay_outline_color,
                                           outline_width=overlay_outline_width)
    return image


# function that returns true if a string is a uuid
def _is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class RenderJobInfo(object):
    def __init__(
        self,
        job_guid,
        camera_guid,
        timelapse_job_info,
        rendering_profile,
        camera_profile,
        temporary_directory,
        snapshot_archive_directory,
        timelapse_directory,
        ffmpeg_directory,
        current_camera_info,
        job_number=0,
        jobs_remaining=0
    ):
        self.ffmpeg_directory = ffmpeg_directory
        self.timelapse_job_info = timelapse_job_info
        self.job_guid = job_guid
        self.camera_guid = camera_guid
        self.job_number = job_number
        self.jobs_remaining = jobs_remaining
        self.camera = camera_profile
        self.camera_info = current_camera_info
        self.temporary_directory = temporary_directory
        self.job_directory = utility.get_temporary_snapshot_job_path(
            self.temporary_directory, job_guid
        )
        self.snapshot_directory = utility.get_temporary_snapshot_job_camera_path(
            self.temporary_directory,
            job_guid,
            camera_guid
        )
        self.snapshot_filename_format = os.path.basename(
            utility.get_snapshot_filename(
                timelapse_job_info.PrintFileName, utility.SnapshotNumberFormat
            )
        )
        self.pre_roll_snapshot_filename_format = utility.get_pre_roll_snapshot_filename(
            timelapse_job_info.PrintFileName, utility.SnapshotNumberFormat
        )
        # rendering directory path
        self.output_tokens = self._get_output_tokens(self.temporary_directory)
        self.rendering_output_format = rendering_profile.output_format
        self.rendering_directory = timelapse_directory
        self.rendering_filename = utility.sanitize_filename(rendering_profile.output_template.format(**self.output_tokens))
        self.rendering_extension = RenderJobInfo._get_extension_from_output_format(rendering_profile.output_format)
        self.rendering_filename_with_extension = "{0}.{1}".format(self.rendering_filename, self.rendering_extension)
        self.rendering_path = os.path.join(
            self.rendering_directory, self.rendering_filename_with_extension
        )
        # snapshot archive path
        self.snapshot_archive_directory = snapshot_archive_directory
        self.snapshot_archive_filename = utility.get_snapshot_archive_filename(self.rendering_filename)
        self.snapshot_archive_path = os.path.join(self.snapshot_archive_directory, self.snapshot_archive_filename)
        self.rendering = rendering_profile
        self.archive_snapshots = self.rendering.archive_snapshots
        # store any rendering errors
        self.rendering_error = None

    def get_snapshot_name_from_index(self, index):
        return utility.get_snapshot_filename(
            self.timelapse_job_info.PrintFileName, index
        )

    def get_snapshot_full_path_from_index(self, index):
        return os.path.join(self.snapshot_directory, self.get_snapshot_name_from_index(index))

    def _get_output_tokens(self, data_directory):
        job_info = self.timelapse_job_info
        assert(isinstance(job_info, utility.TimelapseJobInfo))

        tokens = {}
        print_end_time_string = (
            "UNKNOWN" if job_info.PrintEndTime is None
            else time.strftime("%Y%m%d%H%M%S", time.localtime(job_info.PrintEndTime))
        )
        tokens["PRINTENDTIME"] = print_end_time_string
        print_end_timestamp = (
            "UNKNOWN" if job_info.PrintEndTime is None
            else "{0:d}".format(math.trunc(round(job_info.PrintEndTime, 2) * 100))
        )
        tokens["PRINTENDTIMESTAMP"] = print_end_timestamp
        print_start_time_string = (
            "UNKNOWN" if job_info.PrintStartTime is None
            else time.strftime("%Y%m%d%H%M%S", time.localtime(job_info.PrintStartTime))
        )
        tokens["PRINTSTARTTIME"] = print_start_time_string
        print_start_timestamp = {
            "UNKNOWN" if job_info.PrintStartTime is None
            else "{0:d}".format(math.trunc(round(job_info.PrintStartTime, 2) * 100))
        }
        tokens["PRINTSTARTTIMESTAMP"] = print_start_timestamp
        tokens["DATETIMESTAMP"] = "{0:d}".format(math.trunc(round(time.time(), 2) * 100))
        failed_flag = "FAILED" if job_info.PrintEndState != "COMPLETED" else ""
        tokens["FAILEDFLAG"] = failed_flag
        failed_separator = "_" if job_info.PrintEndState != "COMPLETED" else ""
        tokens["FAILEDSEPARATOR"] = failed_separator
        failed_state = "UNKNOWN" if not job_info.PrintEndState else "" if job_info.PrintEndState == "COMPLETED" else job_info.PrintEndState
        tokens["FAILEDSTATE"] = failed_state
        tokens["PRINTSTATE"] = "UNKNOWN" if not job_info.PrintEndState else job_info.PrintEndState
        tokens["GCODEFILENAME"] = "" if not job_info.PrintFileName else job_info.PrintFileName
        tokens["DATADIRECTORY"] = "" if not data_directory else data_directory
        tokens["SNAPSHOTCOUNT"] = 0
        tokens["CAMERANAME"] = "UNKNOWN" if not self.camera else self.camera.name
        tokens["FPS"] = 0
        return tokens

    @staticmethod
    def _get_extension_from_output_format(output_format):
        EXTENSIONS = {"avi": "avi",
                      "flv": "flv",
                      "h264": "mp4",
                      "vob": "vob",
                      "mp4": "mp4",
                      "mpeg": "mpeg",
                      "gif": "gif"}
        return EXTENSIONS.get(output_format.lower(), "mp4")

    @staticmethod
    def _get_extension_from_output_format(output_format):
        EXTENSIONS = {"avi": "avi",
                      "flv": "flv",
                      "h264": "mp4",
                      "vob": "vob",
                      "mp4": "mp4",
                      "mpeg": "mpeg",
                      "gif": "gif"}
        return EXTENSIONS.get(output_format.lower(), "mp4")


class RenderingProcessor(threading.Thread):
    """Watch for rendering jobs via a rendering queue.  Extract jobs from the queue, and spawn a rendering thread,
       one at a time for each rendering job.  Notify the calling thread of the number of jobs in the queue on demand."""
    def __init__(
        self, rendering_task_queue, data_directory, plugin_version, default_settings_folder,
        octoprint_settings, get_current_settings_callback, on_prerender_start,
        on_start, on_success, on_error, on_end, on_unfinished_renderings_changed, on_in_process_renderings_changed,
        on_unfinished_renderings_loaded
    ):
        super(RenderingProcessor, self).__init__()
        self._plugin_version = plugin_version
        self._default_settings_folder = default_settings_folder
        self._octoprint_settings = octoprint_settings
        self._get_current_settings_callback = get_current_settings_callback
        self._temporary_directory = None
        self._snapshot_archive_directory = None
        self._timelapse_directory = None
        self._ffmpeg_directory = None
        self.r_lock = threading.RLock()
        self.temp_files_lock = threading.RLock()
        self.rendering_task_queue = rendering_task_queue
        # make a local copy of everything.
        self.data_directory = data_directory
        self._on_prerender_start_callback = on_prerender_start
        self._on_start_callback = on_start
        self._on_success_callback = on_success
        self._on_error_callback = on_error
        self._on_end_callback = on_end
        self._on_unfinished_renderings_changed_callback = on_unfinished_renderings_changed
        self._on_in_process_renderings_changed_callback = on_in_process_renderings_changed
        self._on_unfinished_renderings_loaded_callback = on_unfinished_renderings_loaded
        self.job_count = 0
        self._is_processing = False
        self._idle_sleep_seconds = 5  # wait at most 5 seconds for a rendering job from the queue
        self._rendering_job_thread = None
        self._current_rendering_job = None
        # a private dict of rendering jobs by print job ID and camera ID
        self._pending_rendering_jobs = {}
        # private vars to hold unfinished and in-process rendering state
        self._unfinished_renderings = []
        self._unfinished_renderings_size = 0
        self._renderings_in_process = []
        self._renderings_in_process_size = 0
        self._has_working_directories = False
        self.update_directories()

    def is_processing(self):
        with self.r_lock:
            return self._has_pending_jobs() or self.rendering_task_queue.qsize() > 0

    def get_failed(self):
        with self.r_lock:
            return {
                "failed": copy.deepcopy(self._unfinished_renderings),
                "failed_size": self._unfinished_renderings_size,
            }

    def get_in_process(self):
        with self.r_lock:
            return {
                "in_process": copy.deepcopy(self._renderings_in_process),
                "in_process_size": self._renderings_in_process_size,
            }

    def update_directories(self):
        """Returns true if the temporary directory has changed."""
        with self.r_lock:

            # mame sure the directories are tested
            success, errors = self._get_current_settings_callback().main_settings.test_directories(
                self.data_directory,
                self._octoprint_settings.settings.getBaseFolder("timelapse")
            )
            if not success:
                return False, errors

            temporary_directory_changed = False
            snapshot_archive_directory_changed = False
            timelapse_directory_changed = False
            ffmpeg_directory_changed = False

            temporary_directory = self._get_current_settings_callback().main_settings.get_temporary_directory(
                self.data_directory
            )

            if self._temporary_directory != temporary_directory:
                temporary_directory_changed = True
                self._temporary_directory = temporary_directory

            snapshot_archive_directory = self._get_current_settings_callback().main_settings.get_snapshot_archive_directory(
                self.data_directory
            )
            if self._snapshot_archive_directory != snapshot_archive_directory:
                snapshot_archive_directory_changed = True
                self._snapshot_archive_directory = snapshot_archive_directory

            timelapse_directory = self._get_current_settings_callback().main_settings.get_timelapse_directory(
                self._octoprint_settings.settings.getBaseFolder("timelapse")
            )
            if self._timelapse_directory != timelapse_directory:
                timelapse_directory_changed = True
                self._timelapse_directory = timelapse_directory

            ffmpeg_directory = self._octoprint_settings.global_get(["webcam", "ffmpeg"])
            if self._ffmpeg_directory != ffmpeg_directory:
                ffmpeg_directory_changed = True
                self._ffmpeg_directory = ffmpeg_directory

        self._has_working_directories = True

        if temporary_directory_changed:
            self._initialize_unfinished_renderings()

        return True, {
            "temporary_directory_changed": temporary_directory_changed,
            "snapshot_archive_directory_changed": snapshot_archive_directory_changed,
            "timelapse_directory_changed": timelapse_directory_changed,
            "ffmpeg_directory_changed": ffmpeg_directory_changed
        }

    def archive_unfinished_job(self, temporary_directory, job_guid, camera_guid, target_path):
        # do not archive if there is a no archive file.  This means the rendering was created from
        # an archive that already existed.
        if utility.has_no_archive_file(temporary_directory, job_guid, camera_guid):
            return
        with self.temp_files_lock:
            job_directory = utility.get_temporary_snapshot_job_path(temporary_directory, job_guid)
            camera_directory = utility.get_temporary_snapshot_job_camera_path(temporary_directory, job_guid, camera_guid)
            target_directory = utility.get_directory_from_full_path(target_path)

            if not os.path.exists(target_directory):
                try:
                    os.makedirs(target_directory)
                except FileExistsError:
                    pass

            with ZipFile(target_path, 'x') as snapshot_archive:
                # add the job info
                timelapse_info_path = os.path.join(job_directory,
                                                   utility.TimelapseJobInfo.timelapse_info_file_name)
                if os.path.exists(timelapse_info_path):
                    snapshot_archive.write(
                        os.path.join(job_directory,
                                     utility.TimelapseJobInfo.timelapse_info_file_name),
                        os.path.join(job_guid, utility.TimelapseJobInfo.timelapse_info_file_name)
                    )
                for name in os.listdir(camera_directory):
                    file_path = os.path.join(camera_directory, name)
                    if os.path.isfile(file_path):
                        snapshot_archive.write(
                            file_path,
                            os.path.join(job_guid, camera_guid, name)
                        )

    def import_snapshot_archive(self, snapshot_archive_path, prevent_archive=False):
        """Attempt to import one or more snapshot archives in the following form:
           1.  The archive contains images (currently jpg only) in the root.
           2.  The archive is contained within a folder named with a GUID that contains another folder
               named with a GUID.
           Each archive will be imported into its own guid job folder into the temporary directory
        """
        # create our dict of archive files
        archive_files_dict = {}
        temporary_directory = self._temporary_directory
        with self.temp_files_lock:
            if not os.path.isfile(snapshot_archive_path):
                return {
                    'success': False,
                    'error': 'The uploaded archive does not exist'
                }
            root_job_guid = "{0}".format(uuid.uuid4())
            root_camera_guid = "{0}".format(uuid.uuid4())

            with ZipFile(snapshot_archive_path) as zip_file:
                archive_files_temp_dict = {}  # a temporary dict to hold values while we construct the jobs
                for fileinfo in zip_file.infolist():
                    # see if the current item is a directory
                    if not fileinfo.is_dir():
                        parts = utility.split_all(fileinfo.filename)
                        name = os.path.basename(fileinfo.filename).lower()
                        name_without_extension = utility.get_filename_from_full_path(name)
                        extension = utility.get_extension_from_filename(name).lower()
                        item = {
                            "name": name,
                            "fileinfo": fileinfo
                        }
                        location_type = None
                        job_guid = None
                        camera_guid = None
                        file_type = None
                        if len(parts) == 1:
                            job_guid = root_job_guid
                            camera_guid = root_camera_guid
                        elif len(parts) == 2 and _is_valid_uuid(parts[0]):
                            job_guid = parts[0].lower()
                        elif len(parts) == 3 and _is_valid_uuid(parts[0]) and _is_valid_uuid(parts[1]):
                            job_guid = parts[0].lower()
                            camera_guid = parts[1].lower()
                        else:
                            continue

                        if job_guid not in archive_files_temp_dict:
                            archive_files_temp_dict[job_guid] = {
                                'cameras': {},
                                'file': None
                            }
                        if camera_guid and camera_guid not in archive_files_temp_dict[job_guid]["cameras"]:
                            archive_files_temp_dict[job_guid]['cameras'][camera_guid] = []

                        # this file is in the root.  See what kind of file this is
                        if utility.TimelapseJobInfo.is_timelapse_info_file(name):
                            item["name"] = utility.TimelapseJobInfo.timelapse_info_file_name
                            # preserve case of the name, but keep the extension lower case
                            archive_files_temp_dict[job_guid]["file"] = item
                        else:
                            if utility.is_valid_snapshot_extension(extension):
                                # preserve case of the name, but keep the extension lower case
                                file_name = "{0}.{1}".format(name_without_extension, extension)
                            elif CameraInfo.is_camera_info_file(name):
                                file_name = CameraInfo.camera_info_filename
                            elif SnapshotMetadata.is_metadata_file(name):
                                file_name = SnapshotMetadata.METADATA_FILE_NAME
                            elif OctolapseSettings.is_camera_settings_file(name):
                                file_name = OctolapseSettings.camera_settings_file_name
                            elif OctolapseSettings.is_rendering_settings_file(name):
                                file_name = OctolapseSettings.rendering_settings_file_name
                            else:
                                continue
                            item["name"] = file_name
                            archive_files_temp_dict[job_guid]['cameras'][camera_guid].append(item)

                # now replace all of the job guids with  new ones to prevent conflicts with existing unfinished
                # rendering jobs.
                for key in archive_files_temp_dict.keys():
                    archive_files_dict["{0}".format(uuid.uuid4())] = archive_files_temp_dict[key]
                archive_files_temp_dict = {}

                # now create the directories and files and place them in the temp snapshot directory
                for job_guid, job in iteritems(archive_files_dict):
                    job_path = utility.get_temporary_snapshot_job_path(temporary_directory, job_guid)
                    if not os.path.isdir(job_path):
                        os.makedirs(job_path)
                    job_info_file = job["file"]
                    if job_info_file:
                        file_path = os.path.join(job_path, job_info_file["name"])
                        with zip_file.open(job_info_file["fileinfo"]) as info_file:
                            with open(file_path, 'wb') as target_file:
                                target_file.write(info_file.read())
                    for camera_guid, camera in iteritems(job["cameras"]):
                        camera_path = utility.get_temporary_snapshot_job_camera_path(
                            temporary_directory, job_guid, camera_guid
                        )
                        if not os.path.isdir(camera_path):
                            os.makedirs(camera_path)
                        for camera_fileinfo in camera:
                            file_path = os.path.join(camera_path, camera_fileinfo["name"])
                            with zip_file.open(camera_fileinfo["fileinfo"]) as camera_file:
                                with open(file_path, 'wb') as target_file:
                                    target_file.write(camera_file.read())

        # now we should have extracted all of the items, add the job to the queue for these cameras
        has_created_jobs = False
        for job_guid, job in iteritems(archive_files_dict):
            has_created_jobs = True
            for camera_guid, camera in iteritems(job["cameras"]):
                # add this job to the queue as an imported item
                if prevent_archive:
                    # add a file that will signify to the rendering engine that no archive should be created
                    utility.create_no_archive_file(temporary_directory, job_guid, camera_guid)
                parameters = {
                    "job_guid": job_guid,
                    "camera_guid": camera_guid,
                    "action": "import",
                    "rendering_profile": None,
                    "camera_profile": None,
                    "temporary_directory": temporary_directory
                }
                self.rendering_task_queue.put(parameters)

        if has_created_jobs:
            return {
                'success': True
            }

        return {
            'success': False,
            'error': 'No files new jobs were found within the uploaded file.'
        }

    def _get_renderings_in_process(self):
        pending_jobs = {}
        current_rendering_job_guid = self._current_rendering_job.get("job_guid", None)
        current_rendering_camera_guid = self._current_rendering_job.get("camera_id", None)
        with self.r_lock:
            for job_guid in self._pending_rendering_jobs:
                jobs = {}
                for camera_guid in self._pending_rendering_jobs[job_guid]:
                    progress = ""
                    if job_guid == current_rendering_job_guid and camera_guid == current_rendering_camera_guid:
                        progress = self._current_rendering_progress
                    jobs[camera_guid] = {
                        "progress": progress
                    }
                pending_jobs[job_guid] = jobs
        return pending_jobs

    @staticmethod
    def _has_enough_images(path):
        image_count = 0
        for name in os.listdir(path):
            if (
                os.path.isfile(os.path.join(path, name)) and
                utility.is_valid_snapshot_extension(utility.get_extension_from_full_path(name).upper())
            ):
                image_count += 1
                if image_count > 1:
                    return True
        return False

    def _initialize_unfinished_renderings(self):
        """ Removes any snapshot folders that cannot be rendered, returns the ones that can
            Returns: [{'id':guid_val, 'path':path, paths: [{id:guid_val, 'path':path}]}]
        """
        with self.r_lock:
            temporary_directory = self._temporary_directory

        snapshot_path = utility.get_temporary_snapshot_directory(temporary_directory)

        # first clean the temporary folder
        self._clean_temporary_directory(temporary_directory)

        logger.info("Fetching all unfinished renderings and metadata at '%s'.", snapshot_path)

        self._unfinished_renderings_size = 0
        self._unfinished_renderings = []
        paths_to_return_temp = []

        if not os.path.isdir(snapshot_path):
            return
        # test each root level path in the snapshot_path to see if it could contain snapshots and append to the proper list
        for basename in utility.walk_directories(snapshot_path):
            path = os.path.join(snapshot_path, basename)
            if _is_valid_uuid(basename):
                paths_to_return_temp.append({'path': path, 'id': basename, 'paths': []})

        # test each valid subdirectory to see if it is a camera directory
        # containing all necessary settings and at least two jpgs
        for job in paths_to_return_temp:
            is_empty = True
            job_guid = job["id"]
            job_path = job['path']
            # for every job, keep track of paths we want to delete
            delete_paths = []
            for camera_guid in utility.walk_directories(job_path):
                path = os.path.join(job_path, camera_guid)
                if (
                    RenderingProcessor._has_enough_images(path) and
                    _is_valid_uuid(camera_guid)
                ):
                    job['paths'].append({'path': path, 'id': camera_guid})

        # ensure that all paths to return contain at least one subdirectory, else add to paths to delete
        unfinished_size = 0
        for path in paths_to_return_temp:
            if path['paths']:
                for camera_path in path['paths']:
                    rendering_metadata = self._get_metadata_for_rendering_files(
                        path['id'], camera_path["id"], self._temporary_directory
                    )
                    self._unfinished_renderings.append(rendering_metadata)
                    self._unfinished_renderings_size += rendering_metadata["file_size"]

        logger.info("Snapshot folder cleaned.")

    def _delete_snapshots_for_job(self, temporary_directory, job_guid, camera_guid):
        with self.temp_files_lock:
            job_path = utility.get_temporary_snapshot_job_path(temporary_directory, job_guid)
            camera_path = utility.get_temporary_snapshot_job_camera_path(temporary_directory, job_path, camera_guid)
            if os.path.exists(camera_path):
                shutil.rmtree(camera_path)

            # see if the job path is empty, if it is delete that too
            has_files_or_folders = False
            for name in os.listdir(job_path):
                path = os.path.join(job_path, name)
                if os.path.isdir(path) or (
                    os.path.isfile(path) and not utility.TimelapseJobInfo.is_timelapse_info_file(name)):
                    has_files_or_folders = True
                    break

            if not has_files_or_folders:
                shutil.rmtree(job_path)

    def _clean_temporary_directory(self, temporary_directory):
        with self.temp_files_lock:
            snapshot_folder = utility.get_temporary_snapshot_directory(temporary_directory)

            # if the folder doesn't exist, it doesn't need to be cleaned.
            if not os.path.isdir(snapshot_folder):
                return
            logger.info("Cleaning temporary snapshot folders at %s.", temporary_directory)
            # function that returns true if a directory has at least two jpegs
            paths_to_delete = []
            paths_to_examine = []

            # test each root level path in the temporary_directory to see if it could contain snapshots and append to the proper list
            for basename in utility.walk_directories(snapshot_folder):
                path = os.path.join(snapshot_folder, basename)
                if _is_valid_uuid(basename):
                    paths_to_examine.append({'path': path, 'id': basename, 'paths': []})

            # see if the temp archive directory exists

            # test each valid subdirectory to see if it is a camera directory
            # containing all necessary settings and at least two jpgs
            for job in paths_to_examine:
                is_empty = True
                job_guid = job["id"]
                job_path = job['path']
                # for every job, keep track of paths we want to delete
                delete_paths = []
                for camera_guid in utility.walk_directories(job_path):
                    path = os.path.join(job_path, camera_guid)
                    if (
                        RenderingProcessor._has_enough_images(path) and
                        _is_valid_uuid(camera_guid)
                    ):
                        job['paths'].append({'path': path, 'id': camera_guid})
                        # commenting this out.  Used to check for in_process tasks and prevent them from being viewed
                        # as unfinished
                        #if not (job_guid in in_process and camera_guid in in_process[job_guid]):
                        #    job['paths'].append({'path': path, 'id': camera_guid})
                        is_empty = False

                    else:
                        delete_paths.append(path)
                # if we didn't add any paths for this job, just delete the whole job
                if is_empty:
                    delete_paths = [job_path]

                paths_to_delete.extend(delete_paths)

                # delete all paths that cannot be rendered
                for path in paths_to_delete:
                    if os.path.exists(path):
                        try:
                            shutil.rmtree(path)
                        except (PermissionError, FileNotFoundError):
                            logger.exception("Could not remove empty snapshot directories at %s.", path)
                            # ignore these errors.
                            pass

    def _get_in_process_rendering_job(self, job_guid, camera_guid):
        for rendering in self._renderings_in_process:
            if rendering["job_guid"] == job_guid and rendering["camera_guid"] == camera_guid:
                return rendering
        return None

    def _get_unfinished_rendering_job(self, job_guid, camera_guid):
        for rendering in self._unfinished_renderings:
            if rendering["job_guid"] == job_guid and rendering["camera_guid"] == camera_guid:
                return rendering
        return None

    def _get_pending_rendering_job(self, job_guid, camera_guid):
        with self.r_lock:
            job = self._pending_rendering_jobs.get(job_guid, None)
            if job:
                return job.get(camera_guid, None)
        return None

    def _get_metadata_for_rendering_files(self, job_guid, camera_guid, temporary_directory):
        metadata_files = self._get_metadata_files_for_job(job_guid, camera_guid, temporary_directory)
        return self._create_job_metadata(job_guid, camera_guid, metadata_files, temporary_directory)

    def _get_metadata_files_for_job(self, job_guid, camera_guid, temporary_directory):
        with self.temp_files_lock:
            # fetch the job from the pending job list if it exists
            job_path = utility.get_temporary_snapshot_job_path(temporary_directory, job_guid)
            camera_path = utility.get_temporary_snapshot_job_camera_path(temporary_directory, job_path, camera_guid)

            print_job_metadata = utility.TimelapseJobInfo.load(
                temporary_directory, job_guid, camera_guid=camera_guid
            ).to_dict()

            rendering_profile = None
            camera_profile = None
            pending_job = self._get_pending_rendering_job(job_guid, camera_guid)
            if pending_job:
                rendering_profile = pending_job["rendering_profile"]
                camera_profile = pending_job["camera_profile"]

            if camera_profile:
                camera_profile = camera_profile.to_dict()
            else:
                camera_settings_path = os.path.join(camera_path, OctolapseSettings.camera_settings_file_name)
                if os.path.exists(camera_settings_path):
                    try:
                        with open(camera_settings_path, 'r') as settings_file:
                            settings = json.load(settings_file)
                            camera_profile = settings.get("profile", {})
                            camera_profile["guid"] = camera_guid
                    except (OSError, IOError, json.JSONDecodeError) as e:
                        logger.exception("Unable to read camera settings from %s.", camera_settings_path)
            if not camera_profile:
                camera_profile = {
                    "name": "UNKNOWN",
                    "guid": None,
                }

            if rendering_profile:
                rendering_profile = rendering_profile.to_dict()
            else:
                # get the rendering metadata if it exists
                rendering_settings_path = os.path.join(camera_path, OctolapseSettings.rendering_settings_file_name)
                if os.path.exists(rendering_settings_path):
                    try:
                        with open(rendering_settings_path, 'r') as settings_file:
                            settings = json.load(settings_file)
                            rendering_profile = settings.get("profile", {})
                    except (OSError, IOError, json.JSONDecodeError) as e:
                        logger.exception("Unable to read rendering settings from %s.", rendering_settings_path)
            if not rendering_profile:
                rendering_profile = {
                    "guid": None,
                    "name": "UNKNOWN",
                }
            # get the camera info metadata if it exists
            camera_info = CameraInfo.load(self._temporary_directory, job_guid, camera_guid)

            return {
                "print_job": print_job_metadata,
                "camera_profile": camera_profile,
                "rendering_profile": rendering_profile,
                "camera_info": camera_info
            }

    def _create_job_metadata(self, job_guid, camera_guid, metadata_files, temporary_directory):
        print_job_metadata = metadata_files["print_job"]
        camera_profile = metadata_files["camera_profile"]
        rendering_profile = metadata_files["rendering_profile"]
        camera_info = metadata_files["camera_info"]
        rendering_metadata = {}
        rendering_metadata["job_guid"] = job_guid
        rendering_metadata["camera_guid"] = camera_guid
        rendering_metadata["camera_profile_guid"] = camera_profile["guid"]
        job_path = utility.get_temporary_snapshot_job_path(temporary_directory, job_guid)
        rendering_metadata["job_path"] = job_path
        camera_path = utility.get_temporary_snapshot_job_camera_path(temporary_directory, job_guid, camera_guid)
        rendering_metadata["camera_path"] = camera_path
        rendering_metadata["print_start_time"] = print_job_metadata["print_start_time"]
        rendering_metadata["print_end_time"] = print_job_metadata["print_end_time"]
        rendering_metadata["print_end_state"] = print_job_metadata["print_end_state"]
        rendering_metadata["print_file_name"] = print_job_metadata["print_file_name"]
        rendering_metadata["print_file_extension"] = print_job_metadata["print_file_extension"]
        file_size = utility.get_directory_size(camera_path)
        rendering_metadata["file_size"] = file_size
        rendering_metadata["camera_name"] = camera_profile.get("name", "UNKNOWN")

        # get the rendering metadata if it exists
        rendering_metadata["rendering_name"] = rendering_profile.get("name", "UNKNOWN")
        rendering_metadata["rendering_guid"] = rendering_profile.get("guid", None)
        rendering_metadata["rendering_description"] = rendering_profile.get("description", "")

        rendering_metadata["snapshot_count"] = camera_info.snapshot_count
        rendering_metadata["snapshot_attempt"] = camera_info.snapshot_attempt
        rendering_metadata["snapshot_errors_count"] = camera_info.errors_count
        return rendering_metadata

    def _has_pending_jobs(self):
        with self.r_lock:
            return len(self._pending_rendering_jobs) > 0

    def _is_thread_running(self):
        with self.r_lock:
            return self._rendering_job_thread and self._rendering_job_thread.is_alive()

    def run(self):
        # initialize
        self._on_unfinished_renderings_loaded_callback()
        # loop forever, always watching for new tasks to appear in the queue
        while True:
            try:
                # see if there are any rendering tasks.
                rendering_task_info = self.rendering_task_queue.get(True, self._idle_sleep_seconds)
                if rendering_task_info:

                    action = rendering_task_info["action"]
                    if action == "add":
                        # add the job to the queue if it is not already
                        self._add_job(
                            rendering_task_info["job_guid"],
                            rendering_task_info["camera_guid"],
                            rendering_task_info["rendering_profile"],
                            rendering_task_info["camera_profile"],
                            rendering_task_info["temporary_directory"],
                        )
                    elif action == "remove_unfinished":
                        # add the job to the queue if it is not already
                        self._remove_unfinished_job(
                            rendering_task_info["job_guid"],
                            rendering_task_info["camera_guid"],
                            delete=rendering_task_info.get("delete", False),
                        )
                    elif action == "import":
                        self._add_unfinished_job(
                            rendering_task_info["job_guid"],
                            rendering_task_info["camera_guid"],
                            rendering_task_info["rendering_profile"],
                            rendering_task_info["camera_profile"],
                            rendering_task_info["temporary_directory"]
                        )
                    # go ahead and signal that the task queue is finished.  We are using another method
                    # to determine if all rendering jobs are completed.
                self.rendering_task_queue.task_done()
            except queue.Empty:
                pass

            # see if we've finished a task, if so, handle it.
            if not self._is_thread_running() and self._is_processing:
                with self.r_lock:
                    # join the thread and retrieve the finished job
                    finished_job = self._rendering_job_thread.join()
                    # we are done with the thread.
                    self._rendering_job_thread = None
                    # we don't consider a job to be failed for insufficient images.
                    # failed jobs get added to the unfinished renderings list.
                    failed = (
                        finished_job.rendering_error is not None and not
                        (
                            isinstance(finished_job.rendering_error, RenderError)
                            and finished_job.rendering_error.type == "insufficient-images"
                        )
                    )
                    # remove the job from the _pending_rendering_jobs dict
                    self._remove_pending_job(
                        finished_job.job_guid,
                        finished_job.camera_guid,
                        failed=failed)
                    # set our is_processing flag
                    self._is_processing = False
                    self._on_render_end(finished_job.temporary_directory)
                    # see if there are any other jobs remaining
                if not self._has_pending_jobs():
                    # no more jobs, signal rendering completion
                    self._on_all_renderings_ended()

            with self.r_lock:
                if not self._has_pending_jobs() or self._is_processing:
                    continue

            # see if there are any jobs to process.  If there are, process them
            job_info = self._get_next_job_info()
            next_job_job_guid = job_info["job_guid"]
            next_job_camera_guid = job_info["camera_guid"]
            rendering_profile = job_info["rendering_profile"]
            camera_profile = job_info["camera_profile"]
            temporary_directory = job_info["temporary_directory"]

            if next_job_job_guid and next_job_camera_guid:
                if not self._start_job(
                    next_job_job_guid, next_job_camera_guid, rendering_profile, camera_profile, temporary_directory
                ):
                    # the job never started.  Remove it and send an error message.
                    with self.r_lock:
                        self._is_processing = False
                        self._on_render_error(
                            None,
                            "Octolapse was unable to start one of the rendering jobs.  See plugin_octolapse.log for more "
                            "details."
                        )
                        self._remove_pending_job(next_job_job_guid, next_job_camera_guid, failed=True)

    def _add_job(self, job_guid, camera_guid, rendering_profile, camera_profile, temporary_directory):
        """Returns true if the job was added, false if it does not exist"""
        with self.r_lock:
            # see if the job is already pending.  If it is, don't add it again.
            camera_jobs = self._pending_rendering_jobs.get(job_guid, None)
            # The job does not exist, add it.
            if not camera_jobs:
                # make sure the key exists for the current job_guid
                camera_jobs = {}
                self._pending_rendering_jobs[job_guid] = camera_jobs

            if camera_guid in camera_jobs:
                return False

            self._pending_rendering_jobs[job_guid][camera_guid] = {
                'rendering_profile': rendering_profile,
                'camera_profile': camera_profile,
                "temporary_directory": temporary_directory
            }
            # add job to the pending job list
            with self.temp_files_lock:
                metadata = self._get_metadata_for_rendering_files(job_guid, camera_guid, temporary_directory)
            metadata["progress"] = "Pending"
            self._renderings_in_process.append(metadata)
            self._renderings_in_process_size += metadata["file_size"]

            # see if the job is in the unfinished job list
            removed_job = None
            for unfinished_job in self._unfinished_renderings:
                if unfinished_job["job_guid"] == job_guid and unfinished_job["camera_guid"] == camera_guid:
                    # the job is in the list.  Remove it
                    self._unfinished_renderings.remove(unfinished_job)
                    # update the size
                    self._unfinished_renderings_size -= metadata["file_size"]
                    removed_job = unfinished_job
                    break
        if removed_job:
            self._on_unfinished_renderings_changed(unfinished_job, "removed")
        self._on_in_process_renderings_changed(metadata, "added")
        return True

    def _add_unfinished_job(self, job_guid, camera_guid, rendering_profile, camera_profile, temporary_directory):
        with self.r_lock:
            metadata = self._get_metadata_for_rendering_files(job_guid, camera_guid, temporary_directory)
            self._unfinished_renderings_size += metadata["file_size"]
            self._unfinished_renderings.append(metadata)
        self._on_unfinished_renderings_changed(metadata, "added")

    def _remove_unfinished_job(self, job_guid, camera_guid, delete=False):
        """Remove a job from the _pending_rendering_jobs dict if it exists"""
        with self.r_lock:
            job = self._get_unfinished_rendering_job(job_guid, camera_guid)
            if job:
                self._unfinished_renderings.remove(job)
                if delete:
                    self._delete_snapshots_for_job(self._temporary_directory, job_guid, camera_guid)

        self._on_unfinished_renderings_changed(job, "removed")

    def _remove_pending_job(self, job_guid, camera_guid, failed=False):
        removed_job = False

        with self.r_lock:
            # handing removal if it's pending
            if self._get_pending_rendering_job(job_guid, camera_guid):

                camera_jobs = self._pending_rendering_jobs.get(job_guid, None)
                if camera_jobs:
                    # remove the camera job if it exists
                    camera_jobs.pop(camera_guid, None)
                    # remove the print guid key if there are no additional camera jobs
                    if len(camera_jobs) == 0:
                        job = self._pending_rendering_jobs.pop(job_guid, None)

                self._current_rendering_job = None
                # add job to the unfinished job list

                # see if the job is in the in process job list
                removed_job = self._get_in_process_rendering_job(job_guid, camera_guid)
                if removed_job:
                    self._renderings_in_process.remove(removed_job)
                    # update the size
                    self._renderings_in_process_size -= removed_job["file_size"]
                    if failed:
                        self._unfinished_renderings.append(removed_job)
                        self._unfinished_renderings_size += removed_job["file_size"]

        if removed_job:
            self._on_in_process_renderings_changed(removed_job, "removed")
        if failed:
            self._on_unfinished_renderings_changed(removed_job, "added")

    def _get_next_job_info(self):
        """Gets the next job in the _pending_rendering_jobs dict, or returns Null if one does not exist"""
        job_guid = None
        camera_guid = None
        rendering_profile = None
        camera_profile = None
        temporary_directory = None
        if self._has_pending_jobs():
            job_guid = next(iter(self._pending_rendering_jobs))
            camera_jobs = self._pending_rendering_jobs.get(job_guid, None)
            if camera_jobs:
                camera_guid = next(iter(camera_jobs))
                camera_settings = camera_jobs[camera_guid]
                rendering_profile = camera_settings["rendering_profile"]
                camera_profile = camera_settings["camera_profile"]
                temporary_directory = camera_settings["temporary_directory"]

            else:
                logger.error("Could not find any camera jobs for the print job with guid %s.", job_guid)
        return {
            "job_guid": job_guid,
            "camera_guid": camera_guid,
            "rendering_profile": rendering_profile,
            "camera_profile": camera_profile,
            "temporary_directory": temporary_directory

        }

    def _get_pending_rendering_job_count(self):
        job_count = 0
        for job_guid in self._pending_rendering_jobs:
            job_count += len(self._pending_rendering_jobs[job_guid])
        return job_count

    def _get_job_settings(self, job_guid, camera_guid, rendering_profile, camera_profile, temporary_directory):
        """Attempt to load all job settings from the snapshot path"""
        settings = OctolapseSettings(self._plugin_version)
        settings.profiles.cameras = {}
        settings.profiles.renderings = {}

        tmp_rendering_profile, tmp_camera_profile = OctolapseSettings.load_rendering_settings(
            self._plugin_version,
            temporary_directory,
            job_guid,
            camera_guid
        )
        # ensure we have some rendering profile
        if not rendering_profile:
            rendering_profile = tmp_rendering_profile
        if not rendering_profile:
            rendering_profile = self._get_current_settings_callback().profiles.current_rendering()

        # ensure we have some camera profile
        if not camera_profile:
            camera_profile = tmp_camera_profile
        if not camera_profile:
            camera_profile = CameraProfile()
            camera_profile.name = "UNKNOWN"
        timelapse_job_info = utility.TimelapseJobInfo.load(temporary_directory, job_guid, camera_guid=camera_guid)
        camera_info = CameraInfo.load(temporary_directory, job_guid, camera_guid)
        job_number = self.job_count
        jobs_remaining = self._get_pending_rendering_job_count() - 1

        return RenderJobInfo(
            job_guid,
            camera_guid,
            timelapse_job_info,
            rendering_profile,
            camera_profile,
            temporary_directory,
            self._snapshot_archive_directory,
            self._timelapse_directory,
            self._ffmpeg_directory,
            camera_info,
            job_number,
            jobs_remaining

        )

    def _start_job(self, job_guid, camera_guid, rendering_profile, camera_profile, temporary_directory):
        with self.r_lock:
            self._is_processing = True
            try:
                job_info = self._get_job_settings(
                    job_guid, camera_guid, rendering_profile, camera_profile, temporary_directory
                )
            except Exception as e:
                logger.exception("Could not load rendering job settings, skipping.")
                return False

            self.job_count += 1

            has_started = threading.Event()
            self._rendering_job_thread = TimelapseRenderJob(
                job_info,
                has_started,
                self._on_prerender_start,
                self._on_render_start,
                self._on_render_error,
                self._on_render_success,
                self._delete_snapshots_for_job,
                self.archive_unfinished_job
            )
            self._rendering_job_thread.daemon = True
            self._current_rendering_job = self._get_in_process_rendering_job(job_guid, camera_guid)
            self._rendering_job_thread.start()
            has_started.wait()

            return True

    def _on_prerender_start(self, payload):
        logger.info("Sending prerender start message")
        self._current_rendering_job["progress"] = "Pre-Rendering"
        self._on_prerender_start_callback(payload, copy.copy(self._current_rendering_job))

    def _on_render_start(self, payload):
        logger.info("Sending render start message")
        self._current_rendering_job["progress"] = "Rendering"
        self._on_start_callback(payload, copy.copy(self._current_rendering_job))

    def _on_render_error(self, payload, error):
        logger.info("Sending render fail message")
        with self.r_lock:
            job_copy = copy.copy(self._current_rendering_job)
            if self._current_rendering_job:
                job_guid = self._current_rendering_job["job_guid"]
                camera_guid = self._current_rendering_job["camera_guid"]
                self._remove_pending_job(job_guid, camera_guid, failed=True)
                delete = False
                if isinstance(error, RenderError):
                    if error.type in ['insufficient-images']:
                        # no need to delete, this was already deleted by the rendering processor
                        self._remove_unfinished_job(job_guid, camera_guid, delete=False)
        self._on_error_callback(payload, error, job_copy)

    def _on_render_success(self, payload):
        logger.info("Sending render complete message")
        self._on_success_callback(payload, copy.copy(self._current_rendering_job))

    def _on_render_end(self, temporary_directory):
        self._clean_temporary_directory(temporary_directory)

    def _on_all_renderings_ended(self):
        logger.info("Sending render end message")
        self._on_end_callback()

    def _on_unfinished_renderings_changed(self, rendering, change_type):
        self._on_unfinished_renderings_changed_callback(rendering, change_type)

    def _on_in_process_renderings_changed(self, rendering, change_type):
        self._on_in_process_renderings_changed_callback(rendering, change_type)


class TimelapseRenderJob(threading.Thread):
    render_job_lock = threading.RLock()

    def __init__(
        self,
        render_job_info,
        on_start_event,
        on_prerender_start,
        on_render_start,
        on_render_error,
        on_render_success,
        delete_snapshots_callback,
        archive_snapshots_callback
    ):
        super(TimelapseRenderJob, self).__init__()
        assert(isinstance(render_job_info, RenderJobInfo))
        self._render_job_info = render_job_info
        self._on_start_event = on_start_event
        self._fps = None
        self._snapshot_metadata = None
        self._image_count = 0
        self._image_count = 0
        self._max_image_number = 0
        self._images_removed_count = 0
        self._threads = render_job_info.rendering.thread_count
        self._ffmpeg = render_job_info.ffmpeg_directory
        if self._ffmpeg is not None:
            self._ffmpeg = self._ffmpeg.strip()
            if sys.platform == "win32" and not (self._ffmpeg.startswith('"') and self._ffmpeg.endswith('"')):
                self._ffmpeg = "\"{0}\"".format(self._ffmpeg)
        ###########
        # callbacks
        ###########
        self._thread = None
        self._archive_snapshots = render_job_info.archive_snapshots
        # full path of the input
        self._temp_rendering_dir = utility.get_temporary_rendering_directory(render_job_info.temporary_directory)
        self._output_directory = ""
        self._output_filename = ""
        self._output_extension = ""
        self._output_filepath = ""
        # render script errors
        self._before_render_error = None
        self._after_render_error = None
        # callbacks
        self.on_prerender_start = on_prerender_start
        self.on_render_start = on_render_start
        self.on_render_error = on_render_error
        self.on_render_success = on_render_success
        self._delete_snapshots_for_job_callback = delete_snapshots_callback
        self._archive_snapshots_callback = archive_snapshots_callback
        # Temporary directory to store intermediate results of rendering.

    def join(self):
        super(TimelapseRenderJob, self).join()
        return self._render_job_info

    def run(self):
        self._on_start_event.set()
        self._render()

    def _prepare_images(self):
        """Creates a temporary rendering directory and copies all images to it, verifies all image files,
           counts images, and finds the maximum snapshot number
        """
        self._image_count = 0

        # clean any existing temporary files
        self._clear_temporary_files()
        # crete the temp directory
        if not os.path.exists(self._temp_rendering_dir):
            try:
                os.makedirs(self._temp_rendering_dir)
            except FileExistsError:
                pass

        if not os.path.isdir(self._render_job_info.snapshot_directory):
            # No snapshots were created.  Return
            return
        # loop through each file in the snapshot directory
        for name in os.listdir(self._render_job_info.snapshot_directory):
            path = os.path.join(self._render_job_info.snapshot_directory, name)
            # skip non-files and non jpgs
            extension = utility.get_extension_from_full_path(path)
            if not os.path.isfile(path) or not utility.is_valid_snapshot_extension(extension):
                continue

            self._image_count += 1
            img_num = utility.get_snapshot_number_from_path(path)
            if img_num > self._max_image_number:
                self._max_image_number = img_num
            # verify the image and convert if necessary
            TimelapseRenderJob._convert_and_copy_image(path, self._temp_rendering_dir)

        # if we have no camera infos, let's create it now
        if self._render_job_info.camera_info.is_empty:
            self._render_job_info.camera_info.snapshot_attempt = self._max_image_number
            self._render_job_info.camera_info.snapshot_count = self._image_count
            self._render_job_info.camera_info.errors_count = -1

        self._render_job_info.output_tokens["SNAPSHOTCOUNT"] = "{0}".format(self._image_count)

    def _clear_temporary_files(self):
        if os.path.isdir(self._temp_rendering_dir):
            for filename in os.listdir(self._temp_rendering_dir):
                filepath = os.path.join(self._temp_rendering_dir, filename)
                if os.path.isfile(filepath) and filename.upper().endswith(".JPG"):
                    # raise an exception if we can't remove the temporary files.  This could be a problem
                    # if we are cleaning the path before rendering.  After rendering is fine.
                    os.remove(filepath)
            try:
                # remove the directory if it is empty, but don't raise an exception.  It doesn't really matter
                # if the directory is removed, it's just cosmetic
                if not os.listdir(self._temp_rendering_dir):
                    os.rmdir(self._temp_rendering_dir)
            except (PermissionError, FileNotFoundError, OSError):
                logger.exception("Could not remove temporary rendering directory.")
                pass

    def _pre_render(self):
        # read any metadata produced by the timelapse process
        # this is used to create text overlays
        self._read_snapshot_metadata()

        # If there aren't enough images, report an error
        if 0 < self._image_count < 2:
            raise RenderError(
                'insufficient-images',
                "Not enough snapshots were found to generate a timelapse for the '{0}' camera profile.".format(
                    self._render_job_info.camera.name, self._images_removed_count
                )
            )
        if self._image_count == 0:
            raise RenderError(
                'insufficient-images',
                "No snapshots were available for the '{0}' camera profile.".format(self._render_job_info.camera.name)
              )

        # calculate the FPS
        self._calculate_fps()
        if self._fps < 1:
            raise RenderError('framerate-too-low', "The calculated FPS is below 1, which is not allowed. "
                                                   "Please check the rendering settings for Min and Max FPS "
                                                   "as well as the number of snapshots captured.")

        # set the outputs - output directory, output filename, output extension
        self._set_outputs()

    def _pre_render_script(self):
        script_path = self._render_job_info.camera.on_before_render_script.strip()
        if not script_path:
            return
        # Todo:  add the original snapshot directory and template path
        cmd = script.CameraScriptBeforeRender(
            script_path,
            self._render_job_info.camera.name,
            self._temp_rendering_dir,
            self._render_job_info.snapshot_filename_format,
            os.path.join(
                self._temp_rendering_dir,
                self._render_job_info.snapshot_filename_format
            )
        )
        cmd.run()
        if not cmd.success():
            self._before_render_error = RenderError(
                'before_render_script_error',
                "A script occurred while executing executing the before-render script.  Check "
                "plugin_octolapse.log for details. "
            )

    def _post_render_script(self):
        script_path = self._render_job_info.camera.on_after_render_script.strip()
        if not script_path:
            return
        # Todo:  add the original snapshot directory and template path
        cmd = script.CameraScriptAfterRender(
            script_path,
            self._render_job_info.camera.name,
            self._temp_rendering_dir,
            self._render_job_info.snapshot_filename_format,
            os.path.join(
                self._temp_rendering_dir,
                self._render_job_info.snapshot_filename_format
            ),
            self._output_directory,
            self._output_filename,
            self._output_extension,
            self._output_filepath
        )
        cmd.run()
        if not cmd.success():
            self._after_render_error = RenderError(
                'after_render_script_error',
                "A script occurred while executing executing the after-render script.  Check "
                "plugin_octolapse.log for details. "
            )

    @staticmethod
    def _convert_and_copy_image(file_path, target_folder):
        try:
            file_name = os.path.basename(file_path)
            target = os.path.join(target_folder, file_name)
            with Image.open(file_path) as img:
                if img.format not in ["JPEG", "JPEG 2000"]:
                    logger.info(
                        "The image at %s is in %s format.  Attempting to convert to jpeg.",
                        file_path,
                        img.format
                    )
                    with img.convert('RGB') as rgb_img:
                        # save the file with a temp name
                        rgb_img.save(target)
                else:
                    utility.fast_copy(file_path, target)
        except IOError as e:
            logger.exception("The file at path %s is not a valid image file, could not be converted, "
                             "and has been removed.", file_path)

    def _read_snapshot_metadata(self):
        # get the metadata path
        metadata_path = os.path.join(self._render_job_info.snapshot_directory, SnapshotMetadata.METADATA_FILE_NAME)
        # make sure the metadata file exists
        if not os.path.isfile(metadata_path):
            # nothing to do here.  Exit
            return
        # see if the metadata file exists
        logger.info('Reading snapshot metadata from %s', metadata_path)

        try:
            with open(metadata_path, 'r') as metadata_file:
                # read the metadaata and convert it to a dict
                dictreader = DictReader(metadata_file, SnapshotMetadata.METADATA_FIELDS)
                # convert the dict to a list
                self._snapshot_metadata = list(dictreader)
                return
        except IOError as e:
            logger.exception("No metadata exists, skipping metadata processing.")
            # If we fail to read the metadata, it could be that no snapshots were taken.
            # Let's not throw an error and just render without the metadata
            pass

    def _calculate_fps(self):
        self._fps = self._render_job_info.rendering.fps

        if self._render_job_info.rendering.fps_calculation_type == 'duration':

            self._fps = utility.round_to(
                float(self._image_count) / float(self._render_job_info.rendering.run_length_seconds), 0.001)
            if self._fps > self._render_job_info.rendering.max_fps:
                self._fps = self._render_job_info.rendering.max_fps
            elif self._fps < self._render_job_info.rendering.min_fps:
                self._fps = self._render_job_info.rendering.min_fps
            message = (
                "FPS Calculation Type:%s, Fps:%s, NumFrames:%s, "
                "DurationSeconds:%s, Max FPS:%s, Min FPS:%s"
            )
            logger.info(
                message,
                self._render_job_info.rendering.fps_calculation_type,
                self._fps,
                self._image_count,
                self._render_job_info.rendering.run_length_seconds,
                self._render_job_info.rendering.max_fps,
                self._render_job_info.rendering.min_fps
            )
        else:
            logger.info("FPS Calculation Type:%s, Fps:%s", self._render_job_info.rendering.fps_calculation_type, self._fps)
        # Add the FPS to the output tokens
        self._render_job_info.output_tokens["FPS"] = "{0}".format(int(math.ceil(self._fps)))

    def _set_outputs(self):
        # Rendering path info
        logger.info("Setting output paths.")
        self._output_filepath = utility.get_collision_free_filepath(self._render_job_info.rendering_path)
        self._output_filename = utility.get_filename_from_full_path(self._output_filepath)
        self._output_directory = utility.get_directory_from_full_path(self._output_filepath)
        self._output_extension = utility.get_extension_from_full_path(self._output_filepath)
        self._snapshot_archive_path = utility.get_collision_free_filepath(
            self._render_job_info.snapshot_archive_path
        )

    #####################
    # Event Notification
    #####################
    def create_callback_payload(self, return_code, reason):
        return RenderingCallbackArgs(
            reason,
            return_code,
            self._render_job_info.job_guid,
            self._render_job_info.job_directory,
            self._render_job_info.snapshot_directory,
            self._output_directory,
            self._output_filename,
            self._output_extension,
            self._render_job_info.snapshot_archive_path if self._render_job_info.archive_snapshots else None,
            self._image_count,
            self._render_job_info.job_number,
            self._render_job_info.jobs_remaining,
            self._render_job_info.camera.name,
            self._before_render_error,
            self._after_render_error,
            self._render_job_info.timelapse_job_info.PrintFileName,
            self._render_job_info.timelapse_job_info.PrintFileExtension
        )

    def _run_prechecks(self):
        if self._ffmpeg is None:
            raise RenderError('ffmpeg_path', "Cannot create movie, path to ffmpeg is unset. "
                                             "Please configure the ffmpeg path within the "
                                             "'Features->Webcam & Timelapse' settings tab.")

        if self._render_job_info.rendering.bitrate is None:
            raise RenderError('no-bitrate', "Cannot create movie, desired bitrate is unset. "
                                            "Please set the bitrate within the Octolapse rendering profile.")

    def _render(self):
        """Rendering runnable."""

        self._run_prechecks()
        # set an error variable to None, we will return None if there are no problems
        r_error = None
        delete_snapshots = False

        # temporary rendering filepath.  the rendering will be moved after it has been completed
        temp_filepath = os.path.join(
            self._temp_rendering_dir, "{0}.{1}".format(str(uuid.uuid4()), "tmp")
        )
        try:
            logger.info("Starting prerender for camera %s.", self._render_job_info.camera_guid)
            self.on_prerender_start(self.create_callback_payload(0, "Pre-render is starting."))

            self._prepare_images()

            self._pre_render_script()

            self._pre_render()

            try:
                logger.info("Creating the directory at %s", self._output_directory)

                if not os.path.exists(self._output_directory):
                    try:
                        os.makedirs(self._output_directory)
                    except FileExistsError:
                        pass
            except Exception as e:
                raise RenderError('create-render-path',
                                  "Render - An exception was thrown when trying to "
                                  "create the rendering path at: {0}.  Please check "
                                  "the logs (plugin_octolapse.log) for details.".format(self._output_directory),
                                  cause=e)

            watermark_path = None
            if self._render_job_info.rendering.enable_watermark:
                watermark_path = self._render_job_info.rendering.selected_watermark
                if watermark_path == '':
                    logger.error("Watermark was enabled but no watermark file was selected.")
                    watermark_path = None
                elif not os.path.exists(watermark_path):
                    logger.error("Render - Watermark file does not exist.")
                    watermark_path = None
                elif sys.platform == "win32":
                    # Because ffmpeg hiccups on windows' drive letters and backslashes we have to give the watermark
                    # path a special treatment. Yeah, I couldn't believe it either...
                    watermark_path = watermark_path.replace(
                        "\\", "/").replace(":", "\\\\:")

            # Do image preprocessing.  This relies on the original file name, so no renaming before running
            # this function
            self._preprocess_images()

            # rename the images
            self._rename_images()

            # Add pre and post roll.
            self._apply_pre_post_roll()

            # prepare ffmpeg command
            command_str = self._create_ffmpeg_command_string(
                os.path.join(self._temp_rendering_dir, self._render_job_info.snapshot_filename_format),
                temp_filepath,
                watermark=watermark_path
            )
            # rename the output file

            logger.info("Running ffmpeg with command string: %s", command_str)
            self.on_render_start(self.create_callback_payload(0, "Starting to render timelapse."))
            with self.render_job_lock:
                try:
                    p = sarge.run(
                        command_str, stdout=sarge.Capture(), stderr=sarge.Capture())
                    os.rename(temp_filepath, self._output_filepath)
                except Exception as e:
                    raise RenderError('rendering-exception', "ffmpeg failed during rendering of movie. "
                                                             "Please check plugin_octolapse.log for details.",
                                      cause=e)
                if p.returncode != 0:
                    return_code = p.returncode
                    stderr_text = p.stderr.text
                    raise RenderError('return-code', "Could not render movie, got return code %r: %s" % (
                        return_code, stderr_text))

            # run any post rendering scripts
            self._post_render_script()

            if self._archive_snapshots:
                # create the copy directory
                camera_path = self._render_job_info.snapshot_directory
                if not os.path.exists(self._render_job_info.snapshot_archive_directory):
                    try:
                        os.makedirs(self._render_job_info.snapshot_archive_directory)
                    except FileExistsError:
                        pass
                self._archive_snapshots_callback(
                    self._render_job_info.temporary_directory,
                    self._render_job_info.job_guid,
                    self._render_job_info.camera_guid,
                    self._snapshot_archive_path
                )

            delete_snapshots = True

        except Exception as e:
            logger.exception("Rendering Error")
            if isinstance(e, RenderError):
                if e.type == 'insufficient-images':
                    delete_snapshots = True
                r_error = e
            else:
                r_error = RenderError('render-error',
                                      "Unknown render error. Please check plugin_octolapse.log for more details.",
                                      e)
        finally:
            # delete the temp rendering file if it exists.
            if os.path.isfile(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except (OSError, FileNotFoundError, PermissionError):
                    logger.exception("Could not delete a temporary rendering file!")
                    pass

            if delete_snapshots:
                try:
                    self._delete_snapshots_for_job_callback(
                        self._render_job_info.temporary_directory, self._render_job_info.job_guid,
                        self._render_job_info.camera_guid
                    )
                except (PermissionError, FileNotFoundError) as e:
                    # we only want to throw an error if we really were supposed to render this timelapse.
                    # so not for insufficient image errors.  Any folders with errors will be deleted here
                    # on reboot and after renderings complete
                    if not (r_error and isinstance(r_error, RenderError) and r_error.type == 'insufficient-images'):
                        raise e

            try:
                self._clear_temporary_files()
            except (OSError, FileNotFoundError, PermissionError):
                # It's not a huge deal if we can't clean the temporary files at the moment.  Log the error and move on.
                logger.exception("Could not clean temporary rendering files.")
                pass

        if r_error is None:
            self.on_render_success(self.create_callback_payload(0, "Timelapse rendering is complete."))
        else:
            self._render_job_info.rendering_error = r_error
            self.on_render_error(self.create_callback_payload(0, "The render process failed."), r_error)

    def _preprocess_images(self):
        logger.info("Starting preprocessing of images.")
        if self._snapshot_metadata is None:
            logger.warning("No snapshot metadata was found, cannot preprocess images.")
            return
        first_timestamp = float(self._snapshot_metadata[0]['time_taken'])
        for index, data in enumerate(self._snapshot_metadata):
            # TODO:  MAKE SURE THIS WORKS IF THERE ARE ANY ERRORS
            # Variables the user can use in overlay_text_template.format().
            format_vars = {}

            # Extra metadata according to SnapshotMetadata.METADATA_FIELDS.
            format_vars['snapshot_number'] = snapshot_number = int(data['snapshot_number']) + 1
            format_vars['file_name'] = data['file_name']
            format_vars['time_taken_s'] = time_taken = float(data['time_taken'])

            # Verify that the file actually exists.

            file_path = os.path.join(
                self._temp_rendering_dir,
                self._render_job_info.get_snapshot_name_from_index(index)
            )
            if os.path.exists(file_path):
                # Calculate time elapsed since the beginning of the print.
                format_vars['current_time'] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time_taken))
                format_vars['time_elapsed'] = "{}".format(
                    datetime.timedelta(seconds=round(time_taken - first_timestamp))
                )

                # Open the image in Pillow and do preprocessing operations.
                with Image.open(file_path) as img:
                    img = self.add_overlay(img,
                                     text_template=self._render_job_info.rendering.overlay_text_template,
                                     format_vars=format_vars,
                                     font_path=self._render_job_info.rendering.overlay_font_path,
                                     font_size=self._render_job_info.rendering.overlay_font_size,
                                     overlay_location=self._render_job_info.rendering.overlay_text_pos,
                                     overlay_text_alignment=self._render_job_info.rendering.overlay_text_alignment,
                                     overlay_text_valign=self._render_job_info.rendering.overlay_text_valign,
                                     overlay_text_halign=self._render_job_info.rendering.overlay_text_halign,
                                     text_color=self._render_job_info.rendering.get_overlay_text_color(),
                                     outline_color=self._render_job_info.rendering.get_overlay_outline_color(),
                                     outline_width=self._render_job_info.rendering.overlay_outline_width)
                    # Save processed image.
                    output_path = os.path.join(self._temp_rendering_dir, "overlay_img.tmp.jpg")
                    if os.path.exists(output_path):
                        os.remove(output_path)
                    img.save(output_path)
                    os.remove(file_path)
                    shutil.move(output_path, file_path)
            else:
                logger.error("The snapshot at %s does not exist.  Skipping preprocessing.", file_path)
        logger.info("Preprocessing success!")

    def _rename_images(self):
        # First, we need to rename our files, but we have to change the file name so that it won't overwrite any existing files
        image_index = 0
        for filename in sorted(os.listdir(self._temp_rendering_dir)):
            # make sure the file is a jpg image
            if filename.lower().endswith(".jpg"):
                output_path = os.path.join(
                    self._temp_rendering_dir,
                    "{0}.tmp".format(self._render_job_info.get_snapshot_name_from_index(image_index))
                )
                file_path = os.path.join(self._temp_rendering_dir, filename)
                shutil.move(file_path, output_path)
                image_index += 1

        # now loop back through all of the files and remove the .tmp extension
        for filename in os.listdir(self._temp_rendering_dir):
            if filename.endswith(".tmp"):
                output_path = os.path.join(self._temp_rendering_dir, filename[:-4])
                file_path = os.path.join(self._temp_rendering_dir, filename)
                shutil.move(file_path, output_path)

    @staticmethod
    def add_overlay(image, text_template, format_vars, font_path, font_size, overlay_location, overlay_text_alignment,
                    overlay_text_valign, overlay_text_halign, text_color, outline_color, outline_width):
        """Adds an overlay to an image with the given parameters. The image is not mutated.
        :param image: A Pillow RGB image.
        :returns The image with the overlay added."""

        text_color_tuple = tuple(text_color)
        outline_color_tuple = tuple(outline_color)
        # No text to draw.
        if not text_template:
            return image
        text = text_template.format(**format_vars)

        # No font selected
        if not font_path:
            # raise RenderError('overlay-font', "No overlay font was specified when attempting to add overlay.")
            return image
        font = ImageFont.truetype(font_path, size=font_size)

        # Create the image to draw on.
        text_image = Image.new('RGBA', image.size, (255, 255, 255, 0))
        d = ImageDraw.Draw(text_image)

        # Process the text position to improve the alignment.
        if isinstance(overlay_location, string_types):
            overlay_location = json.loads(overlay_location)
        x, y = tuple(overlay_location)
        # valign.
        if overlay_text_valign == 'top':
            pass
        elif overlay_text_valign == 'middle':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            y += image.size[1] / 2 - textsize[1] / 2
        elif overlay_text_valign == 'bottom':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            y += image.size[1] - textsize[1]
        else:
            raise RenderError('overlay-text-valign',
                              "An invalid overlay text valign ({0}) was specified.".format(overlay_text_valign))
        # halign.
        if overlay_text_halign == 'left':
            pass
        elif overlay_text_halign == 'center':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            x += image.size[0] / 2 - textsize[0] / 2
        elif overlay_text_halign == 'right':
            textsize = d.multiline_textsize(text, font=font, spacing=0)
            x += image.size[0] - textsize[0]
        else:
            raise RenderError('overlay-text-halign',
                              "An invalid overlay text halign ({0}) was specified.".format(overlay_text_halign))

        # Draw overlay text.
        d.multiline_text(
            xy=(x, y),
            text=text,
            fill=text_color_tuple,
            font=font,
            align=overlay_text_alignment,
            stroke_width=outline_width,
            stroke_fill=outline_color_tuple

        )


        return Image.alpha_composite(image.convert('RGBA'), text_image).convert('RGB')

    def _apply_pre_post_roll(self):
        # Here we will be adding pre and post roll frames.
        # This routine assumes that images exist, that the first image has number 0, and that
        # there are no missing images
        logger.info("Starting pre/post roll.")
        # start with pre-roll.
        pre_roll_frames = int(self._render_job_info.rendering.pre_roll_seconds * self._fps)
        if pre_roll_frames > 0:
            # We will be adding images starting with -1 and decrementing 1 until we've added the
            # correct number of frames.

            # create a variable to hold the new path of the first image
            first_image_path = os.path.join(
                self._temp_rendering_dir, self._render_job_info.snapshot_filename_format % 0
            )

            # rename all of the current files. The snapshot number should be
            # incremented by the number of pre-roll frames. Start with the last
            # image and work backwards to avoid overwriting files we've already moved

            for image_number in range(pre_roll_frames):
                new_image_path = os.path.join(
                    self._temp_rendering_dir,
                    self._render_job_info.pre_roll_snapshot_filename_format % (0, image_number)
                )
                utility.fast_copy(first_image_path, new_image_path)
        # finish with post
        post_roll_frames = int(self._render_job_info.rendering.post_roll_seconds * self._fps)
        if post_roll_frames > 0:
            last_frame_index = self._image_count - 1
            last_image_path = os.path.join(
                self._temp_rendering_dir, self._render_job_info.snapshot_filename_format % last_frame_index
            )
            for post_roll_index in range(post_roll_frames):
                new_image_path = os.path.join(
                    self._temp_rendering_dir,
                    self._render_job_info.pre_roll_snapshot_filename_format % (last_frame_index, post_roll_index)
                )
                utility.fast_copy(last_image_path, new_image_path)

        if pre_roll_frames > 0:
            # pre or post roll frames were added, so we need to rename all of our images
            self._rename_images()
        logger.info("Pre/post roll generated successfully.")

    @staticmethod
    def _get_vcodec_from_output_format(output_format):
        VCODECS = {"avi": "mpeg4",
                   "flv": "flv1",
                   "gif": "gif",
                   "h264": "h264",
                   "mp4": "mpeg4",
                   "mpeg": "mpeg2video",
                   "vob": "mpeg2video"}
        return VCODECS.get(output_format.lower(), "mpeg2video")

    def _create_ffmpeg_command_string(self, input_file_format, output_file, watermark=None, pix_fmt="yuv420p"):
        """
        Create ffmpeg command string based on input parameters.
        Arguments:
            input_file_format (str): Absolute path to input files including file mask
            output_file (str): Absolute path to output file
            watermark (str): Path to watermark to apply to lower left corner.
            pix_fmt (str): Pixel format to use for output. Default of yuv420p should usually fit the bill.
        Returns:
            (str): Prepared command string to render `input` to `output` using ffmpeg.
        """

        v_codec = self._get_vcodec_from_output_format(self._render_job_info.rendering.output_format)

        command = [self._ffmpeg, '-framerate', "{}".format(self._fps), '-loglevel', 'error', '-i',
                   '"{}"'.format(input_file_format)]
        command.extend(
            ['-threads', "{}".format(self._threads), '-r', "{}".format(self._fps), '-y', '-b', "{}".format(self._render_job_info.rendering.bitrate), '-vcodec', v_codec, '-f', self._render_job_info.rendering_output_format])

        filter_string = self._create_filter_string(watermark=watermark, pix_fmt=pix_fmt)

        if filter_string is not None:
            logger.debug("Applying video filter chain: %s", filter_string)
            command.extend(["-vf", sarge.shell_quote(filter_string)])

        # finalize command with output file
        logger.debug("Rendering movie to %s", output_file)
        command.append('"{}"'.format(output_file))

        return " ".join(command)

    @classmethod
    def _create_filter_string(cls, watermark=None, pix_fmt="yuv420p"):
        """
        Creates an ffmpeg filter string based on input parameters.
        Arguments:
            watermark (str): Path to watermark to apply to lower left corner.
            pix_fmt (str): Pixel format to use, defaults to "yuv420p" which should usually fit the bill
        Returns:
            (str): filter string
        """

        filters = []

        # apply pixel format
        filters.append('[{{prev_filter}}] format={} [{{next_filter}}]'.format(pix_fmt))

        # add watermark if configured
        if watermark is not None:
            filters.append(
                'movie={} [wm]; [{{prev_filter}}][wm] overlay=10:main_h-overlay_h-10 [{{next_filter}}]'.format(
                    watermark))

        # Apply string format to each filter to chain them together.
        filter_names = ['f' + "{}".format(x) for x in range(len(filters))] + ['out']
        for i, previous_filter_name, next_filter_name in zip(range(len(filters)), filter_names, filter_names[1:]):
            filters[i] = filters[i].format(prev_filter=previous_filter_name, next_filter=next_filter_name)
        # Build the final filter string.
        filter_string = "; ".join(filters)

        return filter_string

    @staticmethod
    def _notify_callback(callback, *args, **kwargs):
        """Notifies registered callbacks of type `callback`."""
        if callback is not None and callable(callback):
            callback(*args, **kwargs)


class RenderError(Exception):
    def __init__(self, type, message, cause=None):
        super(RenderError, self).__init__()
        self.type = type
        self.cause = cause if cause is not None else None
        self.message = message

    def __str__(self):
        if self.cause is None:
            return "{}: {}".format(self.type, self.message, "{}".format(self.cause))

        return "{}: {}.  Inner Exception: {}".format(self.type, self.message, "{}".format(self.cause))


class RenderingCallbackArgs(object):
    def __init__(
        self,
        reason,
        return_code,
        job_guid,
        job_directory,
        snapshot_directory,
        rendering_directory,
        rendering_filename,
        rendering_extension,
        archive_path,
        snapshot_count,
        job_number,
        jobs_remaining,
        camera_name,
        before_render_error,
        after_render_error,
        gcode_filename,
        gcode_file_extension
    ):
        self.Reason = reason
        self.ReturnCode = return_code
        self.JobId = job_guid
        self.JobDirectory = job_directory
        self.SnapshotDirectory = snapshot_directory
        self.RenderingDirectory = rendering_directory
        self.RenderingFilename = rendering_filename
        self.RenderingExtension = rendering_extension
        self.ArchivePath = archive_path
        self.SnapshotCount = snapshot_count
        self.JobNumber = job_number
        self.JobsRemaining = jobs_remaining
        self.CameraName = camera_name
        self.BeforeRenderError = before_render_error
        self.AfterRenderError = after_render_error
        self.GcodeFilename = gcode_filename
        self.GcodeFileExtension = gcode_file_extension

    def get_rendering_filename(self):
        return "{0}.{1}".format(self.RenderingFilename, self.RenderingExtension)

    def get_rendering_path(self):
        return os.path.join(self.RenderingDirectory, self.get_rendering_filename())
