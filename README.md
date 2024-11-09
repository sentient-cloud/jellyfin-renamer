# Jellyfin renamer megascript

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

1.  `python3 jellyfin-renamer.py movie --dry-run /path/to/media media_out`

    IF you already have a /path/to/media_out directory, name it something else

    IF it is tv shows, use "show" instead of "movie"

2.  "media_out" is now created in the _parent_ directory of /path/to/media, and contains the new directory structure

3.  verify it looks good

    if a movie/show is missing the [tmdbid=xyz] tag but exists on TMDB,
    means it wasnt found. the name is either misspelled, or the year is incorrect.

    if something is wrong, delete the media_out directory and make necessary adjustments to the media directory

4.  delete the newly created media_out directory

5.  run the script again, without the --dry-run flag

6.  files are now moved. if it failed, you should restore from the backup you made earlier and either fix my shitty script or do it manually

## Requires:

(probably) only works on linux

- python3
- python-requests
- python-Levenshtein
- ffprobe (optional)


## TODO:

- add feature to use metadata from dry run, since library scans are quite s l o w
- rewrite the super shit path parser
- make it idempotent
