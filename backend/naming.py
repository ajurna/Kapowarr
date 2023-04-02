#-*- coding: utf-8 -*-

"""This file contains functions regarding the (re)naming of folders and media
"""

import logging
from os import listdir
from os.path import basename, dirname, isdir, isfile, join, splitext
from re import escape, match
from string import Formatter
from typing import Dict, List

from backend.custom_exceptions import (InvalidSettingValue, IssueNotFound,
                                       VolumeNotFound)
from backend.db import get_db
from backend.files import rename_file
from backend.settings import Settings

formatting_keys = (
	'series_name',
	'clean_series_name',
	'volume_number',
	'comicvine_id',
	'year',
	'publisher'
)

issue_formatting_keys = formatting_keys + (
	'issue_comicvine_id',
	'issue_number',
	'issue_title',
	'issue_release_date'
)

#=====================
# Name generation
#=====================
def _get_formatting_data(volume_id: int, issue_id: int=None) -> dict:
	# Fetch volume data and check if id is valid
	cursor = get_db('dict')
	volume_data = cursor.execute("""
		SELECT
			comicvine_id,
			title, year, publisher,
			volume_number
		FROM volumes
		WHERE id = ?
		LIMIT 1;
	""", (volume_id,)).fetchone()
	
	if not volume_data:
		raise VolumeNotFound
	volume_data = dict(volume_data)
	
	# Build formatted data
	if volume_data.get('title').startswith('The '):
		clean_title = volume_data.get('title') + ', The'
	elif volume_data.get('title').startswith('A '):
		clean_title = volume_data.get('title') + ', A'
	else:
		clean_title = volume_data.get('title') or 'Unknown'
	
	formatting_data = {
		'series_name': volume_data.get('title') or 'Unknown',
		'clean_series_name': clean_title,
		'volume_number': volume_data.get('volume_number') or 'Unknown',
		'comicvine_id': volume_data.get('comicvine_id') or 'Unknown',
		'year': volume_data.get('year') or 'Unknown',
		'publisher': volume_data.get('publisher') or 'Unknown'
	}
	
	if issue_id:
		# Add issue data if issue is found
		issue_data = cursor.execute("""
			SELECT
				comicvine_id,
				issue_number,
				title, date
			FROM issues
			WHERE id = ?;
		""", (issue_id,)).fetchone()
		
		if not issue_data:
			raise IssueNotFound
		issue_data = dict(issue_data)
			
		formatting_data.update({
			'issue_comicvine_id': issue_data.get('comicvine_id') or 'Unknown',
			'issue_number': issue_data.get('issue_number') or 'Unknown',
			'issue_title': issue_data.get('title') or 'Unknown',
			'issue_release_date': issue_data.get('date') or 'Unknown'
		})
		
	return formatting_data

def generate_volume_folder_name(volume_id: int) -> str:
	formatting_data = _get_formatting_data(volume_id)
	format: str = Settings().get_settings()['volume_folder_naming']

	name = format.format(**formatting_data)
	return name

def generate_tpb_name(volume_id: int) -> str:
	formatting_data = _get_formatting_data(volume_id)
	format: str = Settings().get_settings()['file_naming_tpb']

	name = format.format(**formatting_data)
	return name

def generate_issue_range_name(volume_id: int, calculated_issue_number_start: float, calculated_issue_number_end: float) -> str:
	issue_id = get_db().execute("""
		SELECT id
		FROM issues
		WHERE volume_id = ?
			AND calculated_issue_number = ?
		LIMIT 1;
	""", (volume_id, calculated_issue_number_start)).fetchone()[0]
	formatting_data = _get_formatting_data(volume_id, issue_id)
	format: str = Settings().get_settings()['file_naming']
	
	# Override issue number to range
	issue_number_start, issue_number_end = get_db().execute("""
		SELECT issue_number
		FROM issues
		WHERE
			volume_id = ?
			AND
			(
				calculated_issue_number = ?
				OR calculated_issue_number = ?
			)
		ORDER BY calculated_issue_number;
	""", (volume_id, calculated_issue_number_start, calculated_issue_number_end)).fetchall()
	formatting_data['issue_number'] = f'{issue_number_start[0]}-{issue_number_end[0]}'
	
	name = format.format(**formatting_data)
	return name

def generate_issue_name(volume_id: int, issue_id: int) -> str:
	formatting_data = _get_formatting_data(volume_id, issue_id)
	format: str = Settings().get_settings()['file_naming']

	name = format.format(**formatting_data)
	return name

#=====================
# Checking formats
#=====================
def check_format(format: str, type: str) -> None:
	keys = [fn for _, fn, _, _ in Formatter().parse(format) if fn is not None]

	if type in ('file_naming', 'file_naming_tpb'):
		naming_keys = issue_formatting_keys if type == 'file_naming' else formatting_keys
		if r'/' in format or r'\\' in format:
			raise InvalidSettingValue('Path seperator found')
	else:
		naming_keys = formatting_keys

	for format_key in keys:
		if not format_key in naming_keys:
			raise InvalidSettingValue('{' + format_key + '}')
			
	return

#=====================
# Renaming
#=====================
def same_name_indexing(suggested_name: str, current_name: str, folder: str, planned_names: List[Dict[str, str]]) -> str:
	same_names = tuple(
		filter(
			lambda r: match(escape(suggested_name) + r'( \(\d+\))?$', r),
			[splitext(basename(r['after']))[0] for r in planned_names]
		)
	)
	if isdir(folder):
		# Add number to filename if an other file in the dest folder has the same name
		basename_file = splitext(basename(current_name))[0]
		same_names += tuple(
			filter(
				lambda f: not f == basename_file and match(escape(suggested_name) + r'(?: \(\d+\))?$', f),
				[splitext(f)[0] for f in listdir(folder)]
			)
		)
	if same_names:
		i = 0
		while True:
			if not i and not suggested_name in same_names:
				break
			if i and not f"{suggested_name} ({i})" in same_names:
				suggested_name += f" ({i})"
				break
			i += 1

	return suggested_name

def preview_mass_rename(volume_id: int, issue_id: int=None) -> List[Dict[str, str]]:
	result = []
	cursor = get_db('dict')
	# Fetch all files linked to the volume or issue
	if issue_id is None:
		file_infos = cursor.execute("""
			SELECT DISTINCT
				f.id, f.filepath
			FROM
				files f
				INNER JOIN issues_files if
				INNER JOIN issues i
			ON
				i.id = if.issue_id
				AND if.file_id = f.id
				AND i.volume_id = ?;
			""",
			(volume_id,)
		).fetchall()
		root_folder = cursor.execute("""
			SELECT rf.folder
			FROM
				root_folders rf
				JOIN volumes v
			ON
				v.root_folder = rf.id
				AND v.id = ?
			LIMIT 1;
			""",
			(volume_id,)
		).fetchone()[0]
		folder = join(root_folder, generate_volume_folder_name(volume_id))
	else:
		file_infos = cursor.execute("""
			SELECT
				f.id, f.filepath
			FROM
				files f
				INNER JOIN issues_files if
			ON
				if.file_id = f.id
				AND if.issue_id = ?;
			""",
			(issue_id,)
		).fetchall()
		folder = dirname(file_infos[0]['filepath'])

	issues_in_volume = cursor.execute(
		"SELECT COUNT(*) FROM issues WHERE volume_id = ?",
		(volume_id,)
	).fetchone()[0]
	for file in file_infos:
		# Determine what issue(s) the file covers
		if not isfile(file['filepath']):
			continue
		logging.debug(f'Renaming: original filename: {file["filepath"]}')
		
		# Find the issues that the file covers
		issues = cursor.execute("""
			SELECT
				calculated_issue_number, issue_id
			FROM
				issues
				INNER JOIN issues_files
			ON
				id = issue_id
				AND file_id = ?
			ORDER BY calculated_issue_number;
			""",
			(file['id'],)
		).fetchall()
		if len(issues) > 1:
			if len(issues) == issues_in_volume:
				# File is TPB
				suggested_name = generate_tpb_name(volume_id)
			else:
				# File covers multiple issues
				suggested_name = generate_issue_range_name(volume_id, issues[0][0], issues[-1][0])
		else:
			# File covers one issue
			suggested_name = generate_issue_name(volume_id, issues[0][1])

		# Add number to filename if other file has the same name
		suggested_name = same_name_indexing(suggested_name, file['filepath'], folder, result)

		suggested_name = join(folder, suggested_name + splitext(file["filepath"])[1])
		logging.debug(f'Renaming: suggested filename: {suggested_name}')
		if file['filepath'] != suggested_name:
			logging.debug(f'Renaming: added rename')
			result.append({
				'before': file['filepath'],
				'after': suggested_name
			})
		
	return result

def mass_rename(volume_id: int, issue_id: int=None) -> None:
	cursor = get_db()
	renames = preview_mass_rename(volume_id, issue_id)
	if not issue_id and renames:
		cursor.execute(
			"UPDATE volumes SET folder = ? WHERE id = ?",
			(dirname(renames[0]['after']), volume_id)
		)
	for r in renames:
		rename_file(r['before'], r['after'])
		cursor.execute(
			"UPDATE files SET filepath = ? WHERE filepath = ?;",
			(r['after'], r['before'])
		)
	logging.info(f'Renamed volume {volume_id} {f"issue {issue_id}" if issue_id else ""}')
	return