from __future__ import print_function

import os
import os.path
import re
import sys
import glob
import hashlib

from .errorlog import ErrorLog, errorlog
from .imagemanager import image_manager
from .blocks import *


class OriginInfo:
    def __init__(self, file, line):
        self.file = file
        self.line = line

    @property
    def md_file(self):
        if self._md_in_links:
            return self.file+".md"
        return self.file


class DocsGenParser(object):
    _header_pat = re.compile(r"^// ([A-Z][A-Za-z0-9_&-]*( ?[A-Z][A-Za-z0-9_&-]*)?)(\([^)]*\))?:( .*)?$")
    RCFILE = ".openscad_docsgen_rc"
    HASHFILE = ".source_hashes"

    def __init__(self, opts):
        self.opts = opts
        self.target = opts.target
        self.strict = opts.strict
        self.quiet = opts.quiet
        self.file_blocks = []
        self.curr_file_block = None
        self.curr_section = None
        self.curr_item = None
        self.curr_parent = None
        self.ignored_file_pats = []
        self.ignored_files = {}
        self.priority_files = []
        self.priority_groups = []
        self.items_by_name = {}

        sfx = self.target.get_suffix()
        self.TOCFILE = "TOC" + sfx
        self.TOPICFILE = "Topics" + sfx
        self.INDEXFILE = "Index" + sfx
        self.CHEATFILE = "CheatSheet" + sfx
        self.SIDEBARFILE = "_Sidebar" + sfx

        self._reset_header_defs()
        self.read_hashes()

    def _sha256sum(self, filename):
        h = hashlib.sha256()
        b = bytearray(128*1024)
        mv = memoryview(b)
        try:
            with open(filename, 'rb', buffering=0) as f:
                for n in iter(lambda : f.readinto(mv), 0):
                    h.update(mv[:n])
        except FileNotFoundError as e:
            pass
        return h.hexdigest()

    def read_hashes(self):
        """Reads all known file hash values from the hashes file.
        """
        self.file_hashes = {}
        target = self.opts.target
        hashfile = os.path.join(target.docs_dir, self.HASHFILE)
        if os.path.isfile(hashfile):
            try:
                with open(hashfile, "r") as f:
                    for line in f.readlines():
                        filename, hashstr = line.strip().split("|")
                        self.file_hashes[filename] = hashstr
            except ValueError as e:
                print("Corrrupt hashes file.  Ignoring.", file=sys.stderr)
                sys.stderr.flush()
                self.file_hashes = {}

    def matches_hash(self, filename):
        """Returns True if the given file matches it's recorded hash value.
        Updates the hash value in memory for the file if it doesn't match.
        Does NOT save hash values to disk.
        """
        newhash = self._sha256sum(filename)
        if filename not in self.file_hashes:
            self.file_hashes[filename] = newhash
            return False
        oldhash = self.file_hashes[filename]
        if oldhash != newhash:
            self.file_hashes[filename] = newhash
            return False
        return True

    def write_hashes(self):
        """Writes out all known hash values.
        """
        target = self.opts.target
        hashfile = os.path.join(target.docs_dir, self.HASHFILE)
        os.makedirs(os.path.dirname(hashfile), exist_ok=True)
        with open(hashfile, "w") as f:
            for filename, hashstr in self.file_hashes.items():
                f.write("{}|{}\n".format(filename, hashstr))

    def _reset_header_defs(self):
        self.header_defs = {
            # BlockHeader:   (parenttype, nodetype, extras, callback)
            'Status':        ( ItemBlock, LabelBlock, None, self._status_block_cb ),
            'Alias':         ( ItemBlock, LabelBlock, None, self._alias_block_cb ),
            'Aliases':       ( ItemBlock, LabelBlock, None, self._alias_block_cb ),
            'Arguments':     ( ItemBlock, TableBlock, (
                                     ('^<abbr title="These args can be used by position or by name.">By&nbsp;Position</abbr>', 'What it does'),
                                     ('^<abbr title="These args must be used by name, ie: name=value">By&nbsp;Name</abbr>', 'What it does')
                                 ), None
                             ),
        }
        lines = [
            "// DefineHeader(Text): Text",
            "// DefineHeader(Text;ItemOnly): Description",
            "// DefineHeader(BulletList;ItemOnly): Usage",
        ]
        self.parse_lines(lines, src_file="Defaults")
        if os.path.exists(self.RCFILE):
            with open(self.RCFILE, "r") as f:
                lines = ["// " + line for line in f.readlines()]
                self.parse_lines(lines, src_file=self.RCFILE)

    def _status_block_cb(self, title, subtitle, body, origin, meta):
        self.curr_item.deprecated = "DEPRECATED" in subtitle

    def _alias_block_cb(self, title, subtitle, body, origin, meta):
        aliases = [x.strip() for x in subtitle.split(",")]
        self.curr_item.aliases.extend(aliases)
        for alias in aliases:
            self.items_by_name[alias] = self.curr_item

    def _skip_lines(self, lines, line_num=0):
        while line_num < len(lines):
            line = lines[line_num]
            if self.curr_item and not line.startswith("//"):
                self.curr_parent = self.curr_item.parent
                self.curr_item = None
            match = self._header_pat.match(line)
            if match:
                return line_num
            line_num += 1
        if self.curr_item:
            self.curr_parent = self.curr_item.parent
            self.curr_item = None
        return line_num

    def _files_prioritized(self):
        out = []
        found = {}
        for pri_file in self.priority_files:
            for file_block in self.file_blocks:
                if file_block.subtitle == pri_file:
                    found[pri_file] = True
                    out.append(file_block)
        for file_block in self.file_blocks:
            if file_block.subtitle not in found:
                out.append(file_block)
        return out

    def _parse_meta_dict(self, meta):
        meta_dict = {}
        for part in meta.split(';'):
            if "=" in part:
                key, val = part.split('=',1)
            else:
                key, val = part, 1
            meta_dict[key] = val
        return meta_dict

    def _define_blocktype(self, title, meta):
        title = title.strip()
        parentspec = None
        meta = self._parse_meta_dict(meta)

        if "ItemOnly" in meta:
            parentspec = ItemBlock

        if "NumList" in meta:
            self.header_defs[title] = (parentspec, NumberedListBlock, None, None)
        elif "BulletList" in meta:
            self.header_defs[title] = (parentspec, BulletListBlock, None, None)
        elif "Table" in meta:
            if "Headers" not in meta:
                raise DocsGenException("DefineHeader", "Table type is missing Header= option, while declaring block:")
            hdr_meta = meta["Headers"].split("||")
            hdr_sets = [[x.strip() for x in hset.split("|")] for hset in hdr_meta]
            self.header_defs[title] = (parentspec, TableBlock, hdr_sets, None)
        elif "Example" in meta:
            self.header_defs[title] = (parentspec, ExampleBlock, None, None)
        elif "Figure" in meta:
            self.header_defs[title] = (parentspec, FigureBlock, None, None)
        elif "Label" in meta:
            self.header_defs[title] = (parentspec, LabelBlock, None, None)
        elif "Text" in meta:
            self.header_defs[title] = (parentspec, TextBlock, None, None)
        elif "Generic" in meta:
            self.header_defs[title] = (parentspec, GenericBlock, None, None)
        else:
            raise DocsGenException("DefineHeader", "Could not parse target block type, while declaring block:")

    def _mkfilenode(self, origin):
        if not self.curr_file_block:
            self.curr_file_block = FileBlock("LibFile", origin.file, [], origin)
            self.curr_parent = self.curr_file_block
            self.file_blocks.append(self.curr_file_block)

    def _parse_block(self, lines, line_num=0, src_file=None):
        line_num = self._skip_lines(lines, line_num)
        if line_num >= len(lines):
            return line_num
        hdr_line_num = line_num
        line = lines[line_num]
        match = self._header_pat.match(line)
        if not match:
            return line_num
        title = match.group(1)
        meta = match.group(3)[1:-1] if match.group(3) else ""
        subtitle = match.group(4).strip() if match.group(4) else ""
        body = []
        line_num += 1

        first_line = True
        indent = 2
        while line_num < len(lines):
            line = lines[line_num]
            if not line.startswith("//" + (" " * indent)):
                if line.startswith("//  "):
                    raise DocsGenException(title, "Body line has less indentation than first line, while declaring block:")
                break
            line = line[2:]
            if first_line:
                first_line = False
                indent = len(line) - len(line.lstrip())
            line = line[indent:]
            body.append(line.rstrip())
            line_num += 1

        try:
            parent = self.curr_parent
            origin = OriginInfo(src_file, hdr_line_num+1)
            if title == "DefineHeader":
                self._define_blocktype(subtitle, meta)
            elif title == "IgnoreFiles":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if subtitle:
                    body.insert(0,subtitle)
                self.ignored_file_pats.extend([
                    fname.strip() for fname in body
                ])
                self.ignored_files = {}
                for pat in self.ignored_file_pats:
                    files = glob.glob(pat,recursive=True)
                    for fname in files:
                        self.ignored_files[fname] = True
            elif title == "PrioritizeFiles":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if subtitle:
                    body.insert(0,subtitle)
                self.priority_files = [x for line in body for x in glob.glob(line.strip())]
            elif title == "DocsDirectory":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if body:
                    raise DocsGenException(title, "Body not supported, while declaring block:")
                self.opts.docs_dir = subtitle.strip().rstrip("/")
                self.opts.update_target()
            elif title == "ProjectName":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if body:
                    raise DocsGenException(title, "Body not supported, while declaring block:")
                self.opts.project_name = subtitle.strip()
                self.opts.update_target()
            elif title == "TargetProfile":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if body:
                    raise DocsGenException(title, "Body not supported, while declaring block:")
                if not self.opts.set_target(subtitle.strip()):
                    raise DocsGenException(title, "Body not supported, while declaring block:")
                self.opts.target_profile = subtitle.strip()
                self.opts.update_target()
            elif title == "GenerateDocs":
                if origin.file != self.RCFILE:
                    raise DocsGenException(title, "Block disallowed outside of {} file:".format(self.RCFILE))
                if body:
                    raise DocsGenException(title, "Body not supported, while declaring block:")
                if not (
                    self.opts.gen_files or self.opts.gen_toc or self.opts.gen_index or
                    self.opts.gen_topics or self.opts.gen_cheat
                ):
                    # Only use default GeneratedDocs if the command-line doesn't specify any docs
                    # types to generate.
                    for part in subtitle.split(","):
                        orig_part = part.strip()
                        part = orig_part.upper()
                        if part == "FILES":
                            self.opts.gen_files = True
                        elif part == "TOC":
                            self.opts.gen_toc = True
                        elif part == "INDEX":
                            self.opts.gen_index = True
                        elif part == "TOPICS":
                            self.opts.gen_topics = True
                        elif part in ["CHEAT", "CHEATSHEET"]:
                            self.opts.gen_cheat = True
                        elif part == "SIDEBAR":
                            self.opts.gen_sidebar = True
                        else:
                            raise DocsGenException(title, 'Unknown type "{}", while declaring block:'.format(orig_part))
            elif title == "vim" or title == "emacs":
                pass  # Ignore vim and emacs modelines
            elif title in ["File", "LibFile"]:
                if self.curr_file_block:
                    raise DocsGenException(title, "File/Libfile block already specified, while declaring block:")
                self.curr_file_block = FileBlock(title, subtitle, body, origin)
                self.curr_section = None
                self.curr_subsection = None
                self.curr_parent = self.curr_file_block
                self.file_blocks.append(self.curr_file_block)
            elif not self.curr_file_block and self.strict:
                raise DocsGenException(title, "Must declare File or LibFile block before declaring block:")

            elif title == "Section":
                self._mkfilenode(origin)
                self.curr_section = SectionBlock(title, subtitle, body, origin, parent=self.curr_file_block)
                self.curr_subsection = None
                self.curr_parent = self.curr_section
            elif title == "Subsection":
                if not self.curr_section:
                    raise DocsGenException(title, "Must declare a Section before declaring block:")
                if not subtitle:
                    raise DocsGenException(title, "Must provide a subtitle when declaring block:")
                self.curr_subsection = SubsectionBlock(title, subtitle, body, origin, parent=self.curr_section)
                self.curr_parent = self.curr_subsection
            elif title == "Includes":
                self._mkfilenode(origin)
                IncludesBlock(title, subtitle, body, origin, parent=self.curr_file_block)
            elif title == "FileSummary":
                if not subtitle:
                    raise DocsGenException(title, "Must provide a subtitle when declaring block:")
                self._mkfilenode(origin)
                self.curr_file_block.summary = subtitle.strip()
            elif title == "FileGroup":
                if not subtitle:
                    raise DocsGenException(title, "Must provide a subtitle when declaring block:")
                self._mkfilenode(origin)
                self.curr_file_block.group = subtitle.strip()
            elif title == "FileFootnotes":
                if not subtitle:
                    raise DocsGenException(title, "Must provide a subtitle when declaring block:")
                self._mkfilenode(origin)
                self.curr_file_block.footnotes = [
                    [x.strip() for x in part.strip().split('=',1)]
                    for part in subtitle.split(";")
                ]
            elif title == "CommonCode":
                self._mkfilenode(origin)
                self.curr_file_block.common_code.extend(body)
            elif title == "Figure":
                self._mkfilenode(origin)
                FigureBlock(title, subtitle, body, origin, parent=parent, meta=meta)
            elif title == "Example":
                if self.curr_item:
                    ExampleBlock(title, subtitle, body, origin, parent=parent, meta=meta)
            elif title == "Figures":
                self._mkfilenode(origin)
                for lnum, line in enumerate(body):
                    FigureBlock("Figure", subtitle, [line], origin, parent=parent, meta=meta)
                    subtitle = ""
            elif title == "Examples":
                if self.curr_item:
                    for lnum, line in enumerate(body):
                        ExampleBlock("Example", subtitle, [line], origin, parent=parent, meta=meta)
                        subtitle = ""
            elif title in self.header_defs:
                parcls, cls, data, cb = self.header_defs[title]
                if not parcls or isinstance(self.curr_parent, parcls):
                    if cls in (GenericBlock, LabelBlock, TextBlock, NumberedListBlock, BulletListBlock):
                        cls(title, subtitle, body, origin, parent=parent)
                    elif cls == TableBlock:
                        cls(title, subtitle, body, origin, parent=parent, header_sets=data)
                    elif cls in (FigureBlock, ExampleBlock):
                        cls(title, subtitle, body, origin, parent=parent, meta=meta)
                    if cb:
                        cb(title, subtitle, body, origin, meta)

            elif title in ["Constant", "Function", "Module", "Function&Module"]:
                self._mkfilenode(origin)
                if not self.curr_section:
                    self.curr_section = SectionBlock("Section", "", [], origin, parent=self.curr_file_block)
                parent = self.curr_parent = self.curr_section
                if subtitle in self.items_by_name:
                    prevorig = self.items_by_name[subtitle].origin
                    msg = "Previous declaration of `{}` at {}:{}, Redeclared:".format(subtitle, prevorig.file, prevorig.line)
                    raise DocsGenException(title, msg)
                item = ItemBlock(title, subtitle, body, origin, parent=parent)
                self.items_by_name[subtitle] = item
                self.curr_item = item
                self.curr_parent = item
            elif title == "Topics":
                if self.curr_item:
                    TopicsBlock(title, subtitle, body, origin, parent=parent)
            elif title == "See Also":
                if self.curr_item:
                    SeeAlsoBlock(title, subtitle, body, origin, parent=parent)
            else:
                raise DocsGenException(title, "Unrecognized block:")

            if line_num >= len(lines) or not lines[line_num].startswith("//"):
                if self.curr_item:
                    self.curr_parent = self.curr_item.parent
                self.curr_item = None
            line_num = self._skip_lines(lines, line_num)

        except DocsGenException as e:
            errorlog.add_entry(origin.file, origin.line, str(e), ErrorLog.FAIL)

        return line_num

    def get_indexed_names(self):
        """Returns the list of all indexable function/module/constants  by name, in alphabetical order.
        """
        lst = sorted(self.items_by_name.keys())
        for item in lst:
            yield item

    def get_indexed_data(self, name):
        """Given the name of an indexable function/module/constant, returns the parsed data dictionary for that item's documentation.

        Example Results
        ---------------
        {
            "name": "Function&Module",
            "subtitle": "foobar()",
            "body": [],
            "file": "foobar.scad",
            "line": 23,
            "topics": ["Testing", "Metasyntactic"],
            "aliases": ["foob()", "feeb()"],
            "see_also": ["barbaz()", "bazqux()"],
            "usages": [
                {
                    "subtitle": "As function",
                    "body": [
                        "val = foobar(a, b, <c>);",
                        "list = foobar(d, b=);"
                    ]
                }, {
                    "subtitle": "As module",
                    "body": [
                        "foobar(a, b, <c>);",
                        "foobar(d, b=);"
                    ]
                }
            ],
            "description": [
                "When called as a function, this returns the foo of bar.",
                "When called as a module, renders a foo as modified by bar."
            ],
            "arguments": [
                "a = The a argument.",
                "b = The b argument.",
                "c = The c argument.",
                "d = The d argument."
            ],
            "examples": [
                [
                    "foobar(5, 7)"
                ], [
                    "x = foobar(5, 7);",
                    "echo(x);"
                ]
            ]
            "children": [
                {
                    "name": "Extra Anchors",
                    "subtitle": "",
                    "body": [
                        "\"fee\" = Anchors at the fee position.",
                        "\"fie\" = Anchors at the fie position."
                    ]
                }
            ]
        }
        """
        if name in self.items_by_name:
            return self.items_by_name[name].get_data()
        return {}

    def get_all_data(self):
        """Gets all the documentation data parsed so far.

        Sample Results
        ----------
        [
            {
                "name": "LibFile",
                "subtitle":"foobar.scad",
                "body": [
                    "This is the first line of the LibFile body.",
                    "This is the second line of the LibFile body."
                ],
                "includes": [
                    "include <foobar.scad>",
                    "include <bazqux.scad>"
                ],
                "commoncode": [
                    "$fa = 2;",
                    "$fs = 2;"
                ],
                "children": [
                    {
                        "name": "Section",
                        "subtitle": "Metasyntactical Calls",  // If subtitle is "", section is just a placeholder.
                        "body": [
                            "This is the first line of the body of the Section.",
                            "This is the second line of the body of the Section."
                        ],
                        "children": [
                            {
                                "name": "Function&Module",
                                "subtitle": "foobar()",
                                "body": [],
                                "file": "foobar.scad",
                                "line": 23,
                                "topics": ["Testing", "Metasyntactic"],
                                "aliases": ["foob()", "feeb()"],
                                "see_also": ["barbaz()", "bazqux()"],
                                "usages": [
                                    {
                                        "subtitle": "As function",
                                        "body": [
                                            "val = foobar(a, b, <c>);",
                                            "list = foobar(d, b=);"
                                        ]
                                    }, {
                                        "subtitle": "As module",
                                        "body": [
                                            "foobar(a, b, <c>);",
                                            "foobar(d, b=);"
                                        ]
                                    }
                                ],
                                "description": [
                                    "When called as a function, this returns the foo of bar.",
                                    "When called as a module, renders a foo as modified by bar."
                                ],
                                "arguments": [
                                    "a = The a argument.",
                                    "b = The b argument.",
                                    "c = The c argument.",
                                    "d = The d argument."
                                ],
                                "examples": [
                                    [
                                        "foobar(5, 7)"
                                    ],
                                    [
                                        "x = foobar(5, 7);",
                                        "echo(x);"
                                    ],
                                    // ... Next Example
                                ]
                                "children": [
                                    {
                                        "name": "Extra Anchors",
                                        "subtitle": "",
                                        "body": [
                                            "\"fee\" = Anchors at the fee position.",
                                            "\"fie\" = Anchors at the fie position."
                                        ]
                                    }
                                ]
                            },
                            // ... next function/module/constant
                        ]
                    },
                    // ... next section
                ]
            },
            // ... next file
        ]
        """
        return [
            fblock.get_data()
            for fblock in self.file_blocks
        ]

    def parse_lines(self, lines, line_num=0, src_file=None):
        """Parses the given list of strings for documentation comments.

        Parameters
        ----------
        lines : list of str
            The list of strings to parse for documentation comments.
        line_num : int
            The current index into the list of strings of the current line to parse.
        src_file : str
            The name of the source file that this is from.  This is used just for error reporting.
            If true, generates images for example scripts, by running them in OpenSCAD.
        """
        while line_num < len(lines):
            line_num = self._parse_block(lines, line_num, src_file=src_file)

    def parse_file(self, filename, commentless=False):
        """Parses the given file for documentation comments.

        Parameters
        ----------
        filename : str
            The name of the file to parse documentaiton comments from.
        commentless : bool
            If true, treat every line of the file as if it starts with '// '.  This is used for reading docsgen config files.
        """
        if filename in self.ignored_files:
            return
        if not self.quiet:
            print("Parsing {}".format(filename))
        self.curr_file_block = None
        self.curr_section = None
        self._reset_header_defs()
        with open(filename, "r") as f:
            if commentless:
                lines = ["// " + line for line in f.readlines()]
            else:
                lines = f.readlines()
            self.parse_lines(lines, src_file=filename)

    def parse_files(self, filenames, commentless=False):
        """Parses all of the given files for documentation comments.

        Parameters
        ----------
        filenames : list of str
            The list of filenames to parse documentaiton comments from.
        commentless : bool
            If true, treat every line of the files as if they starts with '// '.  This is used for reading docsgen config files.
        """
        for filename in filenames:
            self.parse_file(filename, commentless=commentless)

    def dump_tree(self, nodes, pfx="", maxdepth=6):
        """Dumps debug info to stdout for parsed documentation subtree."""
        if maxdepth <= 0 or not nodes:
            return
        for node in nodes:
            print("{}{}".format(pfx,node))
            for line in node.body:
                print("  {}{}".format(pfx,line))
            self.dump_tree(node.children, pfx=pfx+"  ", maxdepth=maxdepth-1)

    def dump_full_tree(self):
        """Dumps debug info to stdout for all parsed documentation."""
        self.dump_tree(self.file_blocks)

    def write_docs_files(self):
        """Generates the docs files for each source file that has been parsed.
        """
        target = self.opts.target
        if self.opts.test_only:
            for fblock in self.file_blocks:
                lines = fblock.get_file_lines(self, target)
                filename = fblock.subtitle.strip()
                hashmatch = self.matches_hash(filename)
                if self.opts.force or not hashmatch:
                    image_manager.process_requests(test_only=self.opts.test_only)
                else:
                    image_manager.purge_requests()
            return
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        for fblock in self.file_blocks:
            filename = fblock.subtitle.strip()
            outfile = os.path.join(target.docs_dir, fblock.origin.file+target.get_suffix())
            if not self.quiet:
                print("Writing {}...".format(outfile))
            outdir = os.path.dirname(outfile)
            if not os.path.exists(outdir):
                os.makedirs(outdir, mode=0o744, exist_ok=True)
            out = fblock.get_file_lines(self, target)
            out = target.postprocess(out)
            with open(outfile,"w") as f:
                for line in out:
                    f.write(line + "\n")
            if self.opts.gen_imgs:
                hashmatch = self.matches_hash(filename)
                if self.opts.force or not hashmatch:
                    image_manager.process_requests(test_only=self.opts.test_only)
                else:
                    image_manager.purge_requests()
                if not self.opts.test_only:
                    if errorlog.file_has_errors(filename):
                        self.file_hashes.pop(filename)
                    self.write_hashes()

    def write_toc_file(self):
        """Generates the table-of-contents TOC file from the parsed documentation"""
        target = self.opts.target
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        prifiles = self._files_prioritized()
        groups = []
        for fblock in prifiles:
            if fblock.group and fblock.group not in groups:
                groups.append(fblock.group)
        for fblock in prifiles:
            if not fblock.group and fblock.group not in groups:
                groups.append(fblock.group)

        out = target.file_header("Table of Contents")
        out.extend(target.horizontal_rule())
        out.extend(target.section_header("List of Files"))
        for group in groups:
            out.extend(target.block_header(group if group else "Miscellaneous"))
            out.extend(target.bullet_list_start())
            for fnum, fblock in enumerate(prifiles):
                if fblock.group != group:
                    continue
                file = fblock.subtitle
                anch = target.header_link("{}. {}".format(fnum+1, file))
                link = target.get_link(file, anchor=anch, literalize=False)
                marks = "".join(
                    " ({})".format(target.italics(note))
                    for mark, note in fblock.footnotes
                )
                out.extend(target.bullet_list_item("{}{}".format(link, marks)))
                out.append(fblock.summary)
            out.extend(target.bullet_list_end())

        for fnum, fblock in enumerate(prifiles):
            out.extend(fblock.get_tocfile_lines(self.opts.target, n=fnum+1, currfile=self.TOCFILE))

        out = target.postprocess(out)
        outfile = os.path.join(target.docs_dir, self.TOCFILE)
        if not self.quiet:
            print("Writing {}...".format(outfile))
        with open(outfile, "w") as f:
            for line in out:
                f.write(line + "\n")

    def write_topics_file(self):
        """Generates the Topics file from the parsed documentation."""
        target = self.opts.target
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        index_by_letter = {}
        for file_block in self.file_blocks:
            for section in file_block.children:
                if not isinstance(section,SectionBlock):
                    continue
                for item in section.children:
                    if not isinstance(item,ItemBlock):
                        continue
                    names = [item.subtitle]
                    names.extend(item.aliases)
                    for topic in item.topics:
                        ltr = "0" if not topic[0].isalpha() else topic[0].upper()
                        if ltr not in index_by_letter:
                            index_by_letter[ltr] = {}
                        if topic not in index_by_letter[ltr]:
                            index_by_letter[ltr][topic] = []
                        for name in names:
                            index_by_letter[ltr][topic].append( (name, item) )
        ltrs_found = sorted(index_by_letter.keys())
        out = target.file_header("Topic Index")
        out.extend(target.markdown_block([
            "An index of topics, with related functions, modules, and constants."
        ]))
        out.extend(
            target.bullet_list(
                "{0}: {1}".format(
                    target.get_link(ltr, anchor=ltr, literalize=False),
                    ", ".join(
                        target.get_link(
                            target.escape_entities(topic),
                            anchor=target.header_link(topic),
                            literalize=False
                        )
                        for topic in sorted(index_by_letter[ltr].keys())
                    )
                )
                for ltr in ltrs_found
            )
        )
        for ltr in ltrs_found:
            topics = sorted(index_by_letter[ltr].keys())
            out.extend(target.horizontal_rule())
            out.extend(target.section_header(ltr, lev=3))
            for topic in topics:
                itemlist = index_by_letter[ltr][topic]
                out.extend(target.item_header(topic))
                out.extend(target.bullet_list_start())
                sorted_items = sorted(itemlist, key=lambda x: x[0].lower())
                for name, item in sorted_items:
                    out.extend(
                        target.bullet_list_item(
                            "{} (in {})".format(
                                item.get_link(target, label=name, currfile=self.TOPICFILE),
                                target.escape_entities(item.origin.file)
                            )
                        )
                    )
                out.extend(target.bullet_list_end())

        out = target.postprocess(out)
        outfile = os.path.join(target.docs_dir, self.TOPICFILE)
        if not self.quiet:
            print("Writing {}...".format(outfile))
        with open(outfile, "w") as f:
            for line in out:
                f.write(line + "\n")

    def write_index_file(self):
        """Generates the alphabetical function/module/constant Index file from the parsed documentation."""
        target = self.opts.target
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        unsorted_items = []
        for file_block in self.file_blocks:
            for sect in file_block.get_children_by_title("Section"):
                items = [
                    item for item in sect.children
                    if isinstance(item, ItemBlock)
                ]
                for item in items:
                    names = [item.subtitle]
                    names.extend(item.aliases)
                    for name in names:
                        unsorted_items.append( (name, item) )
        sorted_items = sorted(unsorted_items, key=lambda x: x[0].lower())
        index_by_letter = {}
        for name, item in sorted_items:
            ltr = "0" if not name[0].isalpha() else name[0].upper()
            if ltr not in index_by_letter:
                index_by_letter[ltr] = []
            index_by_letter[ltr].append( (name, item ) )
        ltrs_found = sorted(index_by_letter.keys())
        out = target.file_header("Alphabetical Index")
        out.extend(target.markdown_block([
            "An index of Functions, Modules, and Constants by name.",
        ]))
        out.extend(target.markdown_block([
            "  ".join(
                target.get_link(ltr, anchor=ltr, literalize=False)
                for ltr in ltrs_found
            )
        ]))
        for ltr in ltrs_found:
            items = [
                "{} (in {})".format(
                    item.get_link(target, label=name, currfile=self.INDEXFILE),
                    target.escape_entities(item.origin.file)
                )
                for name, item in index_by_letter[ltr]
            ]
            out.extend(target.horizontal_rule())
            out.extend(target.section_header(ltr, lev=3))
            out.extend(target.bullet_list(items))

        out = target.postprocess(out)
        outfile = os.path.join(target.docs_dir, self.INDEXFILE)
        if not self.quiet:
            print("Writing {}...".format(outfile))
        with open(outfile, "w") as f:
            for line in out:
                f.write(line + "\n")

    def write_cheatsheet_file(self):
        """Generates the CheatSheet file from the parsed documentation."""
        target = self.opts.target
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        if target.project_name is None:
            title = "Cheat Sheet"
        else:
            title = "The {} Cheat Sheet".format(target.project_name)
        out = target.file_header(title)
        pri_blocks = self._files_prioritized()
        for file_block in pri_blocks:
            out.extend(file_block.get_cheatsheet_lines(self, self.opts.target))

        out = target.postprocess(out)
        outfile = os.path.join(target.docs_dir, self.CHEATFILE)
        if not self.quiet:
            print("Writing {}...".format(outfile))
        with open(outfile, "w") as f:
            for line in out:
                f.write(line + "\n")

    def write_sidebar_file(self):
        """Generates the _Sidebar index of files from the parsed documentation"""
        target = self.opts.target
        os.makedirs(target.docs_dir, mode=0o744, exist_ok=True)
        prifiles = self._files_prioritized()
        groups = []
        for fblock in prifiles:
            if fblock.group and fblock.group not in groups:
                groups.append(fblock.group)
        for fblock in prifiles:
            if not fblock.group and fblock.group not in groups:
                groups.append(fblock.group)

        footmarks = []
        footnotes = {}
        out = []
        out.append(target.get_link("Table of Contents", file="TOC", literalize=False))
        out.append(target.get_link("Function Index", file="Index", literalize=False))
        out.append(target.get_link("Topics Index", file="Topics", literalize=False))
        out.append(target.get_link("Cheat Sheet", file="CheatSheet", literalize=False))
        out.append(target.get_link("Tutorials", file="Tutorials", literalize=False))
        out.append("")
        out.extend(target.horizontal_rule())
        out.extend(target.section_header("List of Files:", lev=3))
        for group in groups:
            out.extend(target.block_header(group if group else "Miscellaneous"))
            out.extend(target.bullet_list_start())
            for fnum, fblock in enumerate(prifiles):
                if fblock.group != group:
                    continue
                file = fblock.subtitle
                link = target.get_link(file, file=file, literalize=False)
                for mark, note in fblock.footnotes:
                    if mark not in footmarks:
                        footmarks.append(mark)
                    if mark not in footnotes:
                        footnotes[mark] = note
                    elif note != footnotes[mark]:
                        raise DocsGenException("FileFootnotes", 'Footnote "{}" conflicts with previous definition "{}", while declaring block:'.format(note, footnotes[mark]))
                marks = target.footnote_marks(fblock.footnotes)
                out.extend(target.bullet_list_item("{}{}".format(link, marks)))
            out.extend(target.bullet_list_end())
        if footmarks:
            out.append("")
            out.extend(target.horizontal_rule())
            out.extend(target.section_header("File Footnotes:", lev=3))
            for mark in footmarks:
                out.append("{} = {}  ".format(mark, note))

        out = target.postprocess(out)
        outfile = os.path.join(target.docs_dir, self.SIDEBARFILE)
        if not self.quiet:
            print("Writing {}...".format(outfile))
        with open(outfile, "w") as f:
            for line in out:
                f.write(line + "\n")


# footnote_marker = '<sup id="fn_{1}">[{0}](#footnote-{1} "{2}")</sup>'.format(num, num.lower(), title)
# footnote_line = '- <b id="footnote-{1}">{0}</b> {2}[↩](#fn_{1})'.format(num, num.lower(), title)


# vim: expandtab tabstop=4 shiftwidth=4 softtabstop=4 nowrap