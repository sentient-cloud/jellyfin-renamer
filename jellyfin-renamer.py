"""
Jellyfin renamer megascript
    a true pinnacle of engineering

    place all media files in the same directory,
    should look like:
        /path/to/media/
            movie1 (year)/
                movie1.ext
            movie2/
                movie2.ext
                movie2.srt
        
    or for tv shows:
        /path/to/media/
            show1 (year)/
                season 01/
                    show1 - S01E01.ext
                    show1 - S01E02.ext
                season 02/
                    subs/
                        S02E01.en.ext
                        S02E02.spanish.ext
                    show1 - S02E01-02.ext
                    show1 - S02E02.ext
            
            show2/
                show2 - S01E01.ext
                show2 - S01E02.ext
                show2 - S02E01-02.ext

    the format is very flexible, and should work with most naming conventions
    subtitles can also all be in a single folder, as long as they are grouped by the season
    and contain the season/episode number in the full path

    as long as the show/movie name is first in the path, and the path contains the season/episode number,
    this should work out fine.
    
    a TMDB api key is required, and can be set in the environment variable TMDB_API_KEY,
    or in a file, with the path set in the environment variable TMDB_API_KEY_FILE (defaults to ./.tmdb-api-key)
    
    media resolution will be inferred from the path, or using ffprobe if available
    
    i can't exactly guarantee that this will work, since its only tested on my own (small-ish, 7tb) library,
    which was already pretty well organized. (the only real reason i wrote this was to add [tmdbid=xyz] to the filenames)
    
    i heavily recommend running this with the --dry-run flag first, which creates a "fake" directory structure,
    where the media and subtitle files are text files containing some metadata. you should probably also
    backup your media files before running this, (i'd recommend running zfs, and taking a snapshot)
    
    you can use the --no-interact flag to automatically select the first result from TMDB, if there are multiple matches
    
    then run like so:
    1. python3 jellyfin-renamer.py movie --dry-run /path/to/media media_out
        IF you already have a /path/to/media_out directory, name it something else
        IF it is tv shows, use "show" instead of "movie"
    
    2. "media_out" is now created in the *parent* directory of /path/to/media, and contains the new directory structure

    3. verify it looks good
        if a movie/show is missing the [tmdbid=xyz] tag but exists on TMDB,
        means it wasnt found. the name is either misspelled, or the year is incorrect.
        
        if something is wrong, delete the media_out directory and make necessary adjustments to the media directory

    4. delete the newly created media_out directory
    
    5. run the script again, without the --dry-run flag
    
    6. files are now moved. if it failed, you should restore from the backup you made earlier and either fix my shitty script or do it manually

    TODO:
        - add feature to use metadata from dry run, since library scans are quite s l o w
        - rewrite the super shit path parser
        - make it idempotent
"""

import argparse
import json
import os
import pickle
import re
import requests
import shutil
import signal
import subprocess
import sys
import time
import atexit
import Levenshtein

from pathlib import Path

from dataclasses import dataclass, field
from typing import *
from enum import Enum

TMDB_API_KEY_FILE = os.getenv("TMDB_API_KEY_FILE") or "./.tmdb-api-key"


def read_auth_file_or_default():
    if TMDB_API_KEY_FILE == "" or TMDB_API_KEY_FILE is None:
        return os.getenv("TMDB_API_KEY")

    try:
        with open(TMDB_API_KEY_FILE, "r") as f:
            return f.read().strip()
    except:
        return os.getenv("TMDB_API_KEY")


auth_key = read_auth_file_or_default()
auth_header = {"Authorization": f"Bearer {auth_key}"}

re._MAXCACHE = 4096
re_redact_api_key = re.compile("(?<=api_key=)[^&]+")


last_request_time = 0


def do_authed_get_and_handle_err(url: str):
    import time

    global last_request_time

    if last_request_time > 0:
        time_since_last_request = time.time() - last_request_time
        if time_since_last_request < 0.02:
            time.sleep(0.01)

    last_request_time = time.time()

    print(f"do_authed_get_and_handle_err: {re_redact_api_key.sub('***', url)}")

    try:
        obj = json.loads(requests.get(url, headers=auth_header).content)

        if "error" in obj or "errors" in obj:
            return None

        return obj

    except requests.exceptions.ConnectionError:
        print("do_authed_get_and_handle_err: connection error", file=sys.stderr)
        return None
    except requests.exceptions.HTTPError as err:
        print(
            f"do_authed_get_and_handle_err: HTTP error {err.response.status_code}",
            file=sys.stderr,
        )
        return None
    except requests.exceptions.Timeout:
        print("do_authed_get_and_handle_err: HTTP timeout", file=sys.stderr)
        return None
    except requests.exceptions.RequestException:
        print("do_authed_get_and_handle_err: request exception", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print("do_authed_get_and_handle_err: could not parse JSON", file=sys.stderr)
        return None
    except:
        print("do_authed_get_and_handle_err: unhandled error", file=sys.stderr)
        return None


tmdb_genres: Dict[int, str] = {}


def query_all_genres():
    global tmdb_genres

    if len(tmdb_genres) > 0:
        return

    movie_genres = do_authed_get_and_handle_err(
        "https://api.themoviedb.org/3/genre/movie/list"
    )
    show_genres = do_authed_get_and_handle_err(
        "https://api.themoviedb.org/3/genre/tv/list"
    )

    if movie_genres is None or show_genres is None:
        return

    if "genres" not in movie_genres or "genres" not in show_genres:
        return

    # just merge them together, seems the id's are the same
    tmdb_genres = {
        genre["id"]: genre["name"]
        for genre in [*movie_genres["genres"], *show_genres["genres"]]
    }


has_ffprobe = subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0


def ffprobe_width_and_height(path: Path) -> Optional[Tuple[int, int]]:
    if not has_ffprobe:
        return None

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    width, height = result.stdout.strip().split("x")
    return int(width), int(height)


def get_resolution_from_ffprobe(
    widthheight: Optional[Tuple[int, int]]
) -> Optional[str]:
    if widthheight is None:
        return None

    width, _ = widthheight

    if width >= 15360:
        return "16K"
    elif width >= 7680:
        return "8K"
    elif width >= 3840:
        return "4K"
    elif width >= 1920:
        return "1080p"
    elif width >= 1280:
        return "720p"
    elif width >= 854:
        return "480p"
    else:
        return "SD"


no_interact = False
no_caches = False

tmdb_not_found: Set[str] = set()


@dataclass
class TmdbShow:
    name: str
    id: int
    genre_ids: List[int]
    genres: List[str]
    first_air_date: str


tmdb_show_name_cache: Dict[str, List[TmdbShow]] = {}


def query_show(name: str, year: Optional[int]) -> List[TmdbShow]:
    global tmdb_not_found
    global tmdb_show_name_cache

    if name in tmdb_not_found:
        return []

    if name in tmdb_show_name_cache:
        return tmdb_show_name_cache[name]

    year = f"&first_air_date_year={year:04d}" if year is not None else ""
    url = f"https://api.themoviedb.org/3/search/tv?query={name}{year}&include_adult=true&api_key={auth_key}"

    obj = do_authed_get_and_handle_err(url)

    if obj is None:
        tmdb_not_found.add(name)
        return []

    if "results" not in obj:
        tmdb_not_found.add(name)
        return []

    results = obj["results"]

    ret = []
    for result in results:
        id = result["id"] if "id" in result else None
        name = result["name"] if "name" in result else None
        genre_ids = result["genre_ids"] if "genre_ids" in result else None
        first_air_date = (
            result["first_air_date"] if "first_air_date" in result else None
        )

        if id is None or name is None or genre_ids is None or first_air_date is None:
            continue

        genres = [tmdb_genres[genre_id] for genre_id in genre_ids]

        ret.append(TmdbShow(name, id, genre_ids, genres, first_air_date))

    tmdb_show_name_cache[name] = ret

    return ret


@dataclass
class TmdbMovie:
    title: str
    id: int
    genre_ids: List[int]
    genres: List[str]
    release_date: str


tmdb_movie_name_cache: Dict[str, List[TmdbMovie]] = {}


def query_movie(title: str, year: Optional[int]) -> List[TmdbMovie]:
    global tmdb_not_found

    if title in tmdb_not_found:
        return []

    if title in tmdb_movie_name_cache:
        return tmdb_movie_name_cache[title]

    year = f"&primary_release_year={year:04d}" if year is not None else ""
    url = f"https://api.themoviedb.org/3/search/movie?query={title}{year}&include_adult=true&api_key={auth_key}"

    obj = do_authed_get_and_handle_err(url)

    if obj is None:
        tmdb_not_found.add(title)
        return []

    if "results" not in obj:
        tmdb_not_found.add(title)
        return []

    results = obj["results"]

    ret = []
    for result in results:
        id = result["id"] if "id" in result else None
        title = result["title"] if "title" in result else None
        genre_ids = result["genre_ids"] if "genre_ids" in result else None
        release_date = result["release_date"] if "release_date" in result else None

        if id is None or title is None or genre_ids is None or release_date is None:
            continue

        genres = [tmdb_genres[genre_id] for genre_id in genre_ids]

        ret.append(TmdbMovie(title, id, genre_ids, genres, release_date))

    tmdb_movie_name_cache[title] = ret

    return ret


cache_time: Dict[str, int] = {}


def write_caches():
    if no_caches:
        return

    global cache_time
    global tmdb_genres
    global tmdb_show_id_cache
    global tmdb_movie_id_cache
    global tmdb_details_tv_season_cache
    global tmdb_details_movie_cache
    global tmdb_movie_name_cache
    global tmdb_show_name_cache

    def write_cache(name: str, obj: object, ignore_time=False):
        if not ignore_time:
            cache_time[name] = int(time.time())

        with open(name, "wb") as f:
            pickle.dump(obj, f)

    write_cache("all_genres.cache.pickle", tmdb_genres)
    write_cache("tmdb_show_id.cache.pickle", tmdb_show_id_cache)
    write_cache("tmdb_movie_id.cache.pickle", tmdb_movie_id_cache)
    write_cache("tmdb_details_tv_season.cache.pickle", tmdb_details_tv_season_cache)
    write_cache("tmdb_details_movie.cache.pickle", tmdb_details_movie_cache)
    write_cache("tmdb_movie_name.cache.pickle", tmdb_movie_name_cache)
    write_cache("tmdb_show_name.cache.pickle", tmdb_show_name_cache)

    write_cache("cache_time.cache.pickle", cache_time, ignore_time=True)


def load_caches():
    global cache_time
    global tmdb_genres
    global tmdb_show_id_cache
    global tmdb_movie_id_cache
    global tmdb_details_tv_season_cache
    global tmdb_details_movie_cache
    global tmdb_movie_name_cache
    global tmdb_show_name_cache

    def read_cache(name: str, ignore_time=False):
        if not ignore_time:
            if name not in cache_time:
                return {}

            if int(time.time()) - cache_time[name] > 86400:
                return {}

        try:
            with open(name, "rb") as f:
                return pickle.load(f)
        except:
            return {}

    cache_time = read_cache("cache_time.cache.pickle", ignore_time=True)

    tmdb_genres = read_cache("all_genres.cache.pickle")
    tmdb_show_id_cache = read_cache("tmdb_show_id.cache.pickle")
    tmdb_movie_id_cache = read_cache("tmdb_movie_id.cache.pickle")
    tmdb_details_tv_season_cache = read_cache("tmdb_details_tv_season.cache.pickle")
    tmdb_details_movie_cache = read_cache("tmdb_details_movie.cache.pickle")
    tmdb_movie_name_cache = read_cache("tmdb_movie_name.cache.pickle")
    tmdb_show_name_cache = read_cache("tmdb_show_name.cache.pickle")


class MediaType(Enum):
    MOVIE = "movie"
    SHOW = "show"


class ShowType(Enum):
    FEATURETTE = "Featurette"
    SHOW = "Show"
    SAMPLE = "Sample"


class FeaturetteTag(Enum):
    BEHIND_THE_SCENES = "Behind the Scenes"
    INTERVIEW = "Interview"
    MAKING_OF = "Making Of"
    PROMO = "Promo"
    TRAILER = "Trailer"
    TEASER = "Teaser"
    WEBISODE = "Webisode" # only really for parks n rec
    DELETED_SCENE = "Deleted Scene"
    EXTRA = "Extra"


@dataclass
class Show:
    media_type: MediaType = MediaType.SHOW
    title: Optional[str] = None
    name: Optional[str] = None
    extension: str = ""
    show_type: Optional[ShowType] = None
    featurette_tags: List[FeaturetteTag] = field(default_factory=list)
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    resolution: Optional[str] = None
    year: Optional[int] = None
    fullpath: Optional[str] = None
    subtitle_paths: List[str] = field(default_factory=list)
    tmdb_id: Optional[int] = None


remove_parts: List[str] = []


def parse_show_or_movie_path(path: Path, media_type: MediaType) -> Optional[Show]:
    global remove_parts

    show = Show()
    show.media_type = media_type
    show.show_type = ShowType.SHOW
    show.fullpath = str(path)

    extension = path.suffix[1:]
    path: str = str(path)[: -len(extension)]

    show.extension = extension

    if extension not in [
        "mp4",
        "mkv",
        "avi",
        "webm",
        "flv",
        "mov",
        "wmv",
        "m4v",
        "3gp",
        "3g2",
    ]:
        return None

    def replace_separators_with_spaces(s: str) -> str:
        return re.sub(r"[._\s]+", " ", s)

    def remove_disallowed_chars(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9åäöũỹẽß\s\(\)\[\]\-]", "", s)

    def exec_regex(regex: re.Pattern, s: str) -> Tuple[str, Optional[str]]:
        match = regex.search(s)
        if match is None:
            return s, None

        s = re.compile(r"\s+").sub(" ", s[: match.start()] + s[match.end() :])

        return s, match.group(0)

    if len(remove_parts) == 0:
        try:
            with open("extra_disallowed.txt", "r") as f:
                remove_parts = f.read().split("\n")
        except:
            pass

    removed_parts = []

    parts = path.split("/")

    first = True

    while len(parts) > 0:
        part = parts.pop(0)

        part = replace_separators_with_spaces(part)
        part = remove_disallowed_chars(part)

        subparts = re.compile(r"\b-\b").split(part)
        if len(subparts) > 1:
            parts = subparts + parts
            continue

        for removed_part in removed_parts:
            part = part.replace(removed_part, "")

        for disallowed in remove_parts:
            part = re.compile(r"\b" + disallowed + r"\b", re.IGNORECASE).sub("", part)

        if part == "":
            continue

        part, featurette = exec_regex(re.compile(r"featurettes?", re.IGNORECASE), part)

        if featurette is not None:
            show.show_type = ShowType.FEATURETTE
            removed_parts.append(featurette)

        part, sample = exec_regex(re.compile(r"sample", re.IGNORECASE), part)

        if sample is not None:
            show.show_type = ShowType.SAMPLE
            removed_parts.append(sample)

        if show.year is None:
            part, year = exec_regex(re.compile(r"[\[\(]?\d{4}[^p][\]\)]?"), part)
            if year is not None:
                removed_parts.append(year)
                try:
                    show.year = int(year.strip("()[] "))
                except:
                    pass

        if show.season is None or show.episode is None:
            part, season_episode_episode_end = exec_regex(
                re.compile(r"S\d+E\d+\-E\d+", re.IGNORECASE), part
            )
            if season_episode_episode_end is not None:
                removed_parts.append(season_episode_episode_end)
                try:
                    season_episode_episode_end = season_episode_episode_end[1:]
                    season_episode, episode_end = season_episode_episode_end.split("-E")

                    season, episode = season_episode.upper().split("E")

                    show.season = int(season)
                    show.episode = int(episode)
                    show.episode_end = int(episode_end)
                except:
                    pass

        if show.season is None or show.episode is None:
            part, season_episode = exec_regex(
                re.compile(r"S\d+E\d+", re.IGNORECASE), part
            )

            if season_episode is not None:
                removed_parts.append(season_episode)
                try:
                    season_episode = season_episode[1:]
                    season, episode = season_episode.upper().split("E")
                    show.season = int(season)
                    show.episode = int(episode)
                except:
                    pass

        if show.season is None:
            part, season = exec_regex(re.compile(r"season \d+", re.IGNORECASE), part)

            if season is not None:
                removed_parts.append(season)
                try:
                    show.season = int(season[7:])
                except:
                    pass

        if show.resolution is None:
            part, resolution = exec_regex(
                re.compile(r"8k|4k|4320p|2160p|1080p|720p|480p", re.IGNORECASE), part
            )

        if resolution is not None:
            removed_parts.append(resolution)
            if resolution.lower() == "4k":
                show.resolution = "2160p"
            elif resolution.lower() == "8k":
                show.resolution = "4320p"
            else:
                show.resolution = resolution

        if first:
            first = False
            # removed_parts.append(part) # not adding this since its the show name, and sometimes an episode is called the same
            show.title = part.strip()
            continue

        if len(parts) == 0:
            if part.find("(") != -1:
                last_paren = part.rfind("(")
                name, part = part[:last_paren], part[last_paren:]

                show.name = name.strip()
            else:
                found = False
                for disallowed in remove_parts:
                    if (
                        re.compile(r"\b" + disallowed + r"\b", re.IGNORECASE).match(
                            part
                        )
                        is not None
                    ):
                        found = True
                        break

                if not found:
                    show.name = part.strip()

    if show.show_type == ShowType.FEATURETTE:
        _, tag = exec_regex(re.compile(r"behind the", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.BEHIND_THE_SCENES)

        _, tag = exec_regex(re.compile(r"interview", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.INTERVIEW)

        _, tag = exec_regex(re.compile(r"making of", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.MAKING_OF)

        _, tag = exec_regex(re.compile(r"promo", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.PROMO)

        _, tag = exec_regex(re.compile(r"trailer", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.TRAILER)

        _, tag = exec_regex(re.compile(r"teaser", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.TEASER)

        _, tag = exec_regex(re.compile(r"webisode", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.WEBISODE)

        _, tag = exec_regex(re.compile(r"deleted scene", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.DELETED_SCENE)

        _, tag = exec_regex(re.compile(r"extra", re.IGNORECASE), path)
        if tag is not None:
            show.featurette_tags.append(FeaturetteTag.EXTRA)

    return show


tmdb_show_id_cache: Dict[str, int] = {}
tmdb_movie_id_cache: Dict[str, int] = {}

tmdb_details_tv_season_cache: Dict[str, object] = {}
tmdb_details_movie_cache: Dict[str, object] = {}


def query_tmdb_id(show: Show) -> Optional[int]:
    global tmdb_show_id_cache
    global tmdb_not_found

    if show.title in tmdb_show_id_cache:
        return tmdb_show_id_cache[show.title]

    if show.media_type == MediaType.SHOW:
        if show.title in tmdb_not_found:
            return None

        tmdb_shows = query_show(show.title, show.year)

        if len(tmdb_shows) == 0:
            return None

        id = -1

        found = 0
        for tmdb_movie in tmdb_shows:
            if show.year is not None and tmdb_movie.first_air_date[:4] == str(
                show.year
            ):
                id = tmdb_movie.id
                found += 1

        if no_interact and len(tmdb_shows) > 1:
            print("no interact: selecting first movie")
            id = tmdb_shows[0].id
            found = 0

        if (id == -1 or found > 1) and len(tmdb_shows) > 1:
            print(f"Multiple shows found for {show.title} ({show.year})")
            print("Please select one of the following:")
            for i, tmdb_movie in enumerate(tmdb_shows):
                print(
                    f"{i + 1}: {tmdb_movie.name} ({tmdb_movie.first_air_date}) id={tmdb_movie.id} [{', '.join(tmdb_movie.genres)}]"
                )

            while True:
                try:
                    selection = int(input("Selection: ")) - 1
                    if selection < 0 or selection >= len(tmdb_shows):
                        raise ValueError()
                    id = tmdb_shows[selection].id
                    break
                except ValueError:
                    print("Invalid selection")

        else:
            id = tmdb_shows[0].id

        tmdb_show_id_cache[show.title] = id

    else:
        if show.title in tmdb_not_found:
            return None

        tmdb_movies = query_movie(show.title, show.year)

        if len(tmdb_movies) == 0:
            return None

        id = -1

        found = 0
        for tmdb_movie in tmdb_movies:
            if show.year is not None and tmdb_movie.release_date[:4] == str(show.year):
                id = tmdb_movie.id
                found += 1

        if no_interact and len(tmdb_movies) > 1:
            print("no interact: selecting first movie")
            id = tmdb_movies[0].id
            found = 0

        if (id == -1 or found > 1) and len(tmdb_movies) > 1:
            print(f"Multiple movies found for {show.title} ({show.year})")
            print("Please select one of the following:")
            for i, tmdb_movie in enumerate(tmdb_movies):
                print(
                    f"{i + 1}: {tmdb_movie.title} ({tmdb_movie.release_date}) id={tmdb_movie.id} [{', '.join(tmdb_movie.genres)}]"
                )

            while True:
                try:
                    selection = int(input("Selection: ")) - 1
                    if selection < 0 or selection >= len(tmdb_movies):
                        raise ValueError()
                    id = tmdb_movies[selection].id
                    break
                except ValueError:
                    print("Invalid selection")

        else:
            id = tmdb_movies[0].id

        tmdb_movie_id_cache[show.title] = id

    if id == -1:
        return None

    return id


def query_tmdb_details(show: Show) -> Optional[object]:
    global tmdb_details_tv_season_cache
    global tmdb_details_movie_cache
    global tmdb_not_found

    if show.media_type == MediaType.SHOW:
        key = ""

        if show.season is None:
            key = f"{show.title} noseason"
        else:
            key = f"{show.title} S{show.season}"

        if show.title in tmdb_not_found:
            return None

        if key in tmdb_details_tv_season_cache:
            return tmdb_details_tv_season_cache[key]

        show_id = query_tmdb_id(show)

        if show_id is None:
            return None

        show_obj = do_authed_get_and_handle_err(
            f"https://api.themoviedb.org/3/tv/{show_id}?api_key={auth_key}"
        )

        season_obj: object = None

        if show.season is None:
            season_obj = do_authed_get_and_handle_err(
                f"https://api.themoviedb.org/3/tv/{show_id}/season/{show.season}?api_key={auth_key}"
            )
        else:
            season_obj = do_authed_get_and_handle_err(
                f"https://api.themoviedb.org/3/tv/{show_id}/season/{show.season}?api_key={auth_key}"
            )

        if season_obj is None:
            tmdb_not_found.add(show.title)
            return None

        if show_obj is not None and "first_air_date" in show_obj:
            season_obj["first_air_date"] = show_obj["first_air_date"]

        if show_obj is not None and "id" in show_obj:
            season_obj["show_id"] = show_obj["id"]

        tmdb_details_tv_season_cache[key] = season_obj

        return season_obj
    else:
        if show.title in tmdb_details_movie_cache:
            return tmdb_details_movie_cache[show.title]

        if show.title in tmdb_not_found:
            return None

        movie_id = query_tmdb_id(show)

        if movie_id is None:
            return None

        movie_obj = do_authed_get_and_handle_err(
            f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={auth_key}"
        )

        if movie_obj is None:
            tmdb_not_found.add(show.title)
            return None

        tmdb_details_movie_cache[show.title] = movie_obj

        return movie_obj


LANGUAGES: Dict[str, List[str]] = {
    # iso639 language codes, set 1, 2/t, 2/b, 3
    "abkhazian": ["ab", "abk", "abk", "abk"],
    "afar": ["aa", "aar", "aar", "aar"],
    "afrikaans": ["af", "afr", "afr", "afr"],
    "akan": ["ak", "aka", "aka", "aka"],
    "albanian": ["sq", "sqi", "alb", "sqi"],
    "amharic": ["am", "amh", "amh", "amh"],
    "arabic": ["ar", "ara", "ara", "ara"],
    "aragonese": ["an", "arg", "arg", "arg"],
    "armenian": ["hy", "hye", "arm", "hye"],
    "assamese": ["as", "asm", "asm", "asm"],
    "avaric": ["av", "ava", "ava", "ava"],
    "avestan": ["ae", "ave", "ave", "ave"],
    "aymara": ["ay", "aym", "aym", "aym"],
    "azerbaijani": ["az", "aze", "aze", "aze"],
    "bambara": ["bm", "bam", "bam", "bam"],
    "bashkir": ["ba", "bak", "bak", "bak"],
    "basque": ["eu", "eus", "baq", "eus"],
    "belarusian": ["be", "bel", "bel", "bel"],
    "bengali": ["bn", "ben", "ben", "ben"],
    "bislama": ["bi", "bis", "bis", "bis"],
    "bosnian": ["bs", "bos", "bos", "bos"],
    "breton": ["br", "bre", "bre", "bre"],
    "bulgarian": ["bg", "bul", "bul", "bul"],
    "burmese": ["my", "mya", "bur", "mya"],
    "cambodian": ["K", "kuyu", "ki", "kik"],
    "catalan": ["ca", "cat", "cat", "cat"],
    "centralKhmer": ["km", "khm", "khm", "khm"],
    "chamorro": ["ch", "cha", "cha", "cha"],
    "chechen": ["ce", "che", "che", "che"],
    "chichewa": ["ny", "nya", "nya", "nya"],
    "chinese": ["zh", "zho", "chi", "zho"],
    "churchSlavonic": ["cu", "chu", "chu", "chu"],
    "chuvash": ["cv", "chv", "chv", "chv"],
    "cornish": ["kw", "cor", "cor", "cor"],
    "corsican": ["co", "cos", "cos", "cos"],
    "cree": ["cr", "cre", "cre", "cre"],
    "croatian": ["hr", "hrv", "hrv", "hrv"],
    "czech": ["cs", "ces", "cze", "ces"],
    "danish": ["da", "dan", "dan", "dan"],
    "divehi": ["dv", "div", "div", "div"],
    "dutch": ["nl", "nld", "dut", "nld"],
    "dzongkha": ["dz", "dzo", "dzo", "dzo"],
    "english": ["en", "eng", "eng", "eng"],
    "esperanto": ["eo", "epo", "epo", "epo"],
    "estonian": ["et", "est", "est", "est"],
    "ewe": ["ee", "ewe", "ewe", "ewe"],
    "faroese": ["fo", "fao", "fao", "fao"],
    "fijian": ["fj", "fij", "fij", "fij"],
    "finnish": ["fi", "fin", "fin", "fin"],
    "french": ["fr", "fra", "fre", "fra"],
    "fulah": ["ff", "ful", "ful", "ful"],
    "gaelic": ["gd", "gla", "gla", "gla"],
    "galician": ["gl", "glg", "glg", "glg"],
    "ganda": ["lg", "lug", "lug", "lug"],
    "georgian": ["ka", "kat", "geo", "kat"],
    "german": ["de", "deu", "ger", "deu"],
    "greek": ["el", "ell", "gre", "ell"],
    "guarani": ["gn", "grn", "grn", "grn"],
    "gujarati": ["gu", "guj", "guj", "guj"],
    "haitian": ["ht", "hat", "hat", "hat"],
    "hausa": ["ha", "hau", "hau", "hau"],
    "hebrew": ["he", "heb", "heb", "heb"],
    "herero": ["hz", "her", "her", "her"],
    "hindi": ["hi", "hin", "hin", "hin"],
    "hiriMotu": ["ho", "hmo", "hmo", "hmo"],
    "hungarian": ["hu", "hun", "hun", "hun"],
    "icelandic": ["is", "isl", "ice", "isl"],
    "ido": ["io", "ido", "ido", "ido"],
    "igbo": ["ig", "ibo", "ibo", "ibo"],
    "indonesian": ["id", "ind", "ind", "ind"],
    "interlingua": ["ia", "ina", "ina", "ina"],
    "interlingue": ["ie", "ile", "ile", "ile"],
    "inuktitut": ["iu", "iku", "iku", "iku"],
    "inupiaq": ["ik", "ipk", "ipk", "ipk"],
    "irish": ["ga", "gle", "gle", "gle"],
    "italian": ["it", "ita", "ita", "ita"],
    "japanese": ["ja", "jpn", "jpn", "jpn"],
    "javanese": ["jv", "jav", "jav", "jav"],
    "kalaallisut": ["kl", "kal", "kal", "kal"],
    "kannada": ["kn", "kan", "kan", "kan"],
    "kanuri": ["kr", "kau", "kau", "kau"],
    "kashmiri": ["ks", "kas", "kas", "kas"],
    "kazakh": ["kk", "kaz", "kaz", "kaz"],
    "kikuyu": ["rw", "kin", "kin", "kin"],
    "kirghiz": ["ky", "kir", "kir", "kir"],
    "komi": ["kv", "kom", "kom", "kom"],
    "kongo": ["kg", "kon", "kon", "kon"],
    "korean": ["ko", "kor", "kor", "kor"],
    "kuanyama": ["kj", "kua", "kua"],
    "kurdish": ["ku", "kur", "kur", "kur"],
    "lao": ["lo", "lao", "lao", "lao"],
    "latin": ["la", "lat", "lat", "lat"],
    "latvian": ["lv", "lav", "lav", "lav"],
    "limburgan": ["li", "lim", "lim", "lim"],
    "lingala": ["ln", "lin", "lin", "lin"],
    "lithuanian": ["lt", "lit", "lit", "lit"],
    "lubaKatanga": ["lu", "lub", "lub", "lub"],
    "luxembourgish": ["lb", "ltz", "ltz", "ltz"],
    "macedonian": ["mk", "mkd", "mac", "mkd"],
    "malagasy": ["mg", "mlg", "mlg", "mlg"],
    "malay": ["ms", "msa", "may", "msa"],
    "malayalam": ["ml", "mal", "mal", "mal"],
    "maltese": ["mt", "mlt", "mlt", "mlt"],
    "manx": ["gv", "glv", "glv", "glv"],
    "maori": ["mi", "mri", "mao", "mri"],
    "marathi": ["mr", "mar", "mar", "mar"],
    "marshallese": ["mh", "mah", "mah", "mah"],
    "mongolian": ["mn", "mon", "mon", "mon"],
    "nauru": ["na", "nau", "nau", "nau"],
    "navajo": ["nv", "nav", "nav", "nav"],
    "ndonga": ["ng", "ndo", "ndo", "ndo"],
    "nepali": ["ne", "nep", "nep", "nep"],
    "northernSami": ["se", "sme", "sme", "sme"],
    "northNdebele": ["nd", "nde", "nde", "nde"],
    "norwegian": ["no", "nor", "nor", "nor"],
    "occitan": ["oc", "oci", "oci", "oci"],
    "ojibwa": ["oj", "oji", "oji", "oji"],
    "oriya": ["or", "ori", "ori", "ori"],
    "oromo": ["om", "orm", "orm", "orm"],
    "ossetian": ["os", "oss", "oss", "oss"],
    "pali": ["pi", "pli", "pli", "pli"],
    "pashto": ["ps", "pus", "pus", "pus"],
    "persian": ["fa", "fas", "per", "fas"],
    "polish": ["pl", "pol", "pol", "pol"],
    "portuguese": ["pt", "por", "por", "por"],
    "punjabi": ["pa", "pan", "pan", "pan"],
    "quechua": ["qu", "que", "que", "que"],
    "romanian": ["ro", "ron", "rum", "ron"],
    "romansh": ["rm", "roh", "roh", "roh"],
    "rundi": ["rn", "run", "run", "run"],
    "russian": ["ru", "rus", "rus", "rus"],
    "samoan": ["sm", "smo", "smo", "smo"],
    "sango": ["sg", "sag", "sag", "sag"],
    "sanskrit": ["sa", "san", "san", "san"],
    "sardinian": ["sc", "srd", "srd", "srd"],
    "serbian": ["sr", "srp", "srp", "srp"],
    "shona": ["sn", "sna", "sna", "sna"],
    "sichuanYi": ["ii", "iii", "iii", "iii"],
    "sindhi": ["sd", "snd", "snd", "snd"],
    "sinhala": ["si", "sin", "sin", "sin"],
    "slovak": ["sk", "slk", "slo", "slk"],
    "slovenian": ["sl", "slv", "slv", "slv"],
    "somali": ["so", "som", "som", "som"],
    "southernSotho": ["st", "sot", "sot", "sot"],
    "southNdebele": ["nr", "nbl", "nbl", "nbl"],
    "spanish": ["es", "spa", "spa", "spa"],
    "sundanese": ["su", "sun", "sun", "sun"],
    "swahili": ["sw", "swa", "swa", "swa"],
    "swati": ["ss", "ssw", "ssw", "ssw"],
    "swedish": ["sv", "swe", "swe", "swe"],
    "tagalog": ["tl", "tgl", "tgl", "tgl"],
    "tahitian": ["ty", "tah", "tah", "tah"],
    "tajik": ["tg", "tgk", "tgk", "tgk"],
    "tamil": ["ta", "tam", "tam", "tam"],
    "tatar": ["tt", "tat", "tat", "tat"],
    "telugu": ["te", "tel", "tel", "tel"],
    "thai": ["th", "tha", "tha", "tha"],
    "tibetan": ["bo", "bod", "tib", "bod"],
    "tigrinya": ["ti", "tir", "tir", "tir"],
    "tonga": ["to", "ton", "ton", "ton"],
    "tsonga": ["ts", "tso", "tso", "tso"],
    "tswana": ["tn", "tsn", "tsn", "tsn"],
    "turkish": ["tr", "tur", "tur", "tur"],
    "turkmen": ["tk", "tuk", "tuk", "tuk"],
    "twi": ["tw", "twi", "twi", "twi"],
    "uighur": ["ug", "uig", "uig", "uig"],
    "ukrainian": ["uk", "ukr", "ukr", "ukr"],
    "urdu": ["ur", "urd", "urd", "urd"],
    "uzbek": ["uz", "uzb", "uzb", "uzb"],
    "venda": ["ve", "ven", "ven", "ven"],
    "vietnamese": ["vi", "vie", "vie", "vie"],
    "volapuk": ["vo", "vol", "vol", "vol"],
    "walloon": ["wa", "wln", "wln", "wln"],
    "welsh": ["cy", "cym", "wel", "cym"],
    "westernfrisian": ["fy", "fry", "fry", "fry"],
    "wolof": ["wo", "wol", "wol", "wol"],
    "xhosa": ["xh", "xho", "xho", "xho"],
    "yiddish": ["yi", "yid", "yid", "yid"],
    "yoruba": ["yo", "yor", "yor", "yor"],
    "zhuang": ["za", "zha", "zha", "zha"],
    "zulu": ["zu", "zul", "zul", "zul"],
}


def handle_signal(signum, _):
    print(f"Got signal {signum}, writing caches and exiting..")
    write_caches()
    sys.exit(0)


if __name__ == "__main__":

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

    atexit.register(write_caches)

    if auth_key is None or auth_key == "":
        print(
            "No TMDB API key found. Provide one by setting the TMDB_API_KEY environment variable or by creating a file named .tmdb-api-key in the process working directory. You may change the location of the file by setting the TMDB_API_KEY_FILE environment variable."
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="jellyfin-renamer.py",
        description="Rename media files and create new directory structure",
    )

    parser.add_argument(
        "media", choices=["movie", "show"], help="Type of media to rename"
    )

    parser.add_argument(
        "--no-cache", action="store_true", help="do not use caches", default=False
    )

    parser.add_argument(
        "--no-interact",
        action="store_true",
        help="do not prompt for user input",
        default=False,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="create a fake directory structure, and do not touch any files",
        default=False,
    )

    parser.add_argument("path", help="path to media files")
    parser.add_argument(
        "output", help="output directory name, relative to the input parent path"
    )

    args = parser.parse_args()

    dry_run = args.dry_run

    path = args.path
    abspath = os.path.abspath(path)

    out_name = args.output
    out_abspath: Path = Path(abspath).parent / out_name

    media_type = args.media

    no_interact = args.no_interact
    no_caches = args.no_cache

    print("Media path:  ", abspath)
    print("Output path: ", str(out_abspath))

    if not no_interact:
        ok = input("OK? (Y/n, default: Y): ")

        if ok.lower() == "n":
            sys.exit(0)

    # check if path exists and is a directory
    if not os.path.exists(abspath):
        print(f"Path {path} does not exist")
        sys.exit(1)

    if not os.path.isdir(abspath):
        print(f"Path {path} is not a directory")
        sys.exit(1)

    if not no_caches:
        load_caches()

    query_all_genres()

    shows: List[Show] = []

    discovered = 0

    for root, _, files in os.walk(abspath):
        for file in files:
            discovered += 1

            if discovered % 100 == 0:
                print(f"Discovered {discovered} files...")

            full_show_path = f"{root}/{file}"
            show_path = full_show_path[len(abspath) + 1 :]

            show = parse_show_or_movie_path(
                Path(show_path),
                MediaType.SHOW if media_type == "show" else MediaType.MOVIE,
            )

            if show is None:
                continue

            if show.resolution is None:
                widthheight = ffprobe_width_and_height(Path(full_show_path))
                show.resolution = get_resolution_from_ffprobe(widthheight)

            dir = Path(full_show_path).parent

            # the greateest code evewr
            for root2, _, files in os.walk(dir):
                for file in files:
                    if (
                        file.endswith(".srt")
                        or file.endswith(".vtt")
                        or file.endswith(".ass")
                        or file.endswith(".mks")
                    ):
                        show.subtitle_paths.append(
                            f"{root2}/{file}"[len(abspath) + 1 :]
                        )

            # if there are multiple subs, and some of them match the name,
            # there was probably a dir with all of them, so then filter them
            # down to the ones with matching episode/season
            if len(show.subtitle_paths) > 1:
                matches_name = False
                for sub in show.subtitle_paths:
                    if sub.find(show.title) or (
                        show.name is not None and sub.find(show.name)
                    ):
                        matches_name = True
                        break

                old_subs = show.subtitle_paths[:]
                for sub in old_subs:
                    season = f"S{show.season:02d}" if show.season is not None else None
                    episode = (
                        f"E{show.episode:02d}" if show.episode is not None else None
                    )

                    season_episode = ""
                    if season is not None:
                        season_episode += season

                    if episode is not None:
                        season_episode += episode

                    if (
                        sub.find(season_episode) != -1
                        or sub.find(season_episode.lower()) != -1
                    ):
                        show.subtitle_paths = [sub]
                        break

            shows.append(show)

    print(f"Discovered {discovered} files")

    new_paths: Dict[str, Show] = {}

    print("Querying TMDB for details...")
    queried = 0

    for show in shows:
        queried += 1

        if queried % 100 == 0:
            print(f"Queried {queried} files...")

        if media_type == "show":
            tmdb_details = query_tmdb_details(show)

            if tmdb_details is None:
                tmdb_details = {}

            if "episodes" in tmdb_details:
                episodes = tmdb_details["episodes"]

                for episode in episodes:
                    if (
                        "episode_number" in episode
                        and episode["episode_number"] == show.episode
                    ):
                        show.name = episode["name"]
                        break

            if show.year is None:
                if "first_air_date" in tmdb_details:
                    show.year = int(tmdb_details["first_air_date"][:4])

            if show.tmdb_id is None:
                if "show_id" in tmdb_details:
                    show.tmdb_id = int(tmdb_details["show_id"])

            new_path = f"{show.title}"
            if show.year is not None:
                new_path += f" ({show.year})"

            if show.tmdb_id is not None:
                new_path += f" [tmdbid-{show.tmdb_id}]"

            if show.season is not None:
                new_path += f"/Season {show.season:02d}"

            if (
                show.show_type == ShowType.SHOW
                and show.season is not None
                and show.episode is not None
            ):
                episode = f"E{show.episode:02d}"
                if show.episode_end is not None:
                    episode += f"-{show.episode_end:02d}"

                name = f"- {show.name}" if show.name is not None else ""

                new_path += f"/{show.title} - S{show.season:02d}{episode} {name} [{show.resolution}].{show.extension}"
            elif show.show_type == ShowType.SAMPLE:
                new_path += f"/samples/{show.name}.{show.extension}"
            elif show.show_type == ShowType.FEATURETTE:
                if len(show.featurette_tags) == 0:
                    new_path += "/featurettes"
                else:
                    if FeaturetteTag.BEHIND_THE_SCENES in show.featurette_tags:
                        new_path += "/behind the scenes"
                    elif FeaturetteTag.INTERVIEW in show.featurette_tags:
                        new_path += "/interview"
                    elif FeaturetteTag.MAKING_OF in show.featurette_tags:
                        new_path += "/behind the scenes"
                    elif FeaturetteTag.DELETED_SCENE in show.featurette_tags:
                        new_path += "/deleted scenes"
                    elif FeaturetteTag.EXTRA in show.featurette_tags:
                        new_path += "/extras"
                    elif FeaturetteTag.PROMO in show.featurette_tags:
                        new_path += "/other"
                    elif FeaturetteTag.TEASER in show.featurette_tags:
                        new_path += "/other"
                    elif FeaturetteTag.TRAILER in show.featurette_tags:
                        new_path += "/trailers"
                    elif FeaturetteTag.WEBISODE in show.featurette_tags:
                        new_path += "/featurettes"
                    else:
                        new_path += "/other"

                new_path += f"/{show.name}.{show.extension}"

            new_paths[new_path] = show

        else:
            tmdb_details = query_tmdb_details(show)

            if tmdb_details is None:
                tmdb_details = {}

            if show.year is None:
                if "release_date" in tmdb_details:
                    show.year = int(tmdb_details["release_date"][:4])

            if show.tmdb_id is None:
                if "id" in tmdb_details:
                    show.tmdb_id = int(tmdb_details["id"])

            new_path = f"{show.title}"

            if show.year is not None:
                new_path += f" ({show.year})"

            if show.tmdb_id is not None:
                new_path += f" [tmdbid-{show.tmdb_id}]"

            new_path += f"/{show.title} - [{show.resolution}].{show.extension}"

            new_paths[new_path] = show

    print(f"Queried {queried} files")

    def process_sub_names(show: Show, new_video_name: str) -> List[str]:
        global remove_parts

        new_subs = []

        lang_keys = list(LANGUAGES.keys())

        for sub in show.subtitle_paths:
            sub_ext = Path(sub).suffix[1:]
            sub_name = Path(sub).stem

            if Path(show.fullpath).stem in sub_name:
                sub_name = re.compile(
                    re.escape(Path(show.fullpath).stem), re.IGNORECASE
                ).sub("", sub_name)

            sub_name = re.compile(r"[.\-_,;:]").sub(" ", sub_name).lower()

            for disallowed in remove_parts:
                sub_name = re.compile(r"\b" + disallowed + r"\b", re.IGNORECASE).sub(
                    "", sub_name
                )

            min_dist = 1000000
            min_lang = None
            found = False

            if len(sub_name) < 2:
                min_lang = "english"
                found = True

            parts = sub_name.split(" ")

            is_sdh = False

            if not found:
                for part in parts:
                    if part == "sdh":
                        is_sdh = True
                        continue

                    if len(part) < 2:
                        continue

                    for lang, codes in LANGUAGES.items():
                        if part in codes:
                            min_lang = lang
                            found = True
                            break

                    if found:
                        break

                    for lang in lang_keys:
                        dist = Levenshtein.distance(part, lang, weights=(1, 20, 100))
                        if dist > 10:
                            continue

                        if dist < min_dist:
                            min_dist = dist
                            min_lang = lang
                            found = True

            new_sub_name = f"{new_video_name}."

            sub_code = LANGUAGES.get(min_lang, LANGUAGES["english"])[0]
            if sub_code == "en":
                sub_code = "default"

            new_sub_name += f"{sub_code}."

            if "forced" in sub_name:
                new_sub_name += "forced."

            if is_sdh:
                new_sub_name += "sdh."

            new_sub_name += f"{sub_ext}"

            new_subs.append(new_sub_name)

        return new_subs

    from pprint import pprint

    f = open("output.txt", "w")

    renamed = 0

    for new_path, show in new_paths.items():
        renamed += 1

        if renamed % 100 == 0:
            print(f"Renamed {renamed} files...")

        new_path = re.compile(r"[\:*?\"<>|]").sub("", new_path)

        path = out_abspath / Path(new_path)
        dir = path.parent

        new_video_name = path.stem

        os.makedirs(dir, exist_ok=True)

        subs = process_sub_names(show, new_video_name)

        if dry_run:
            newpath = Path(path).with_suffix(f".{show.extension}.txt")
            with open(newpath, "w") as f:
                f.write(f"fullpath: {show.fullpath}\n")
                f.write(f"newpath: {new_path}\n")
                pprint(show, stream=f)

            for i, sub in enumerate(subs):
                subpath = Path(newpath).parent / sub
                subpath = Path(str(subpath) + ".txt")

                with open(subpath, "w") as f:
                    f.write(f"fullpath: {show.subtitle_paths[i]}\n")
                    f.write(f"newpath: {sub}\n")
        else:
            show_abspath = Path(abspath) / Path(show.fullpath)
            show_newpath = Path(path).with_suffix(f".{show.extension}")

            print("  ", show_abspath, file=f)
            print("->", show_newpath, file=f)
            shutil.move(show_abspath, show_newpath)

            for i, sub in enumerate(subs):
                sub_abspath = Path(abspath) / Path(show.subtitle_paths[i])
                sub_newpath = Path(dir) / Path(sub)

                print("  ", sub_abspath, file=f)
                print("->", sub_newpath, file=f)
                shutil.move(sub_abspath, sub_newpath)

            print("", file=f)

    print(f"Renamed {renamed} files")

    f.close()

    print("Done")
