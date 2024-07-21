""" MKDocs Build Plantuml Plugin """
import base64
from pathlib import Path
import httplib2
import re
import six
import string
import zlib
from lxml import etree
import tempfile
import shutil


from mkdocs.config import config_options, base
from mkdocs.plugins import BasePlugin
import mkdocs.structure.files
from subprocess import call

if six.PY2:
    from string import maketrans
else:
    maketrans = bytes.maketrans


plantuml_alphabet = (
    string.digits + string.ascii_uppercase + string.ascii_lowercase + "-_"
)
base64_alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/"
b64_to_plantuml = maketrans(
    base64_alphabet.encode("utf-8"), plantuml_alphabet.encode("utf-8")
)


class BuildPlantumlPluginConfig(base.Config):
    """
    Configuration class for the BuildPlantumlPlugin.

    Attributes:
        render (str): The rendering mode for PlantUML diagrams. Default is "server".
        server (str): The URL of the PlantUML server. Default is "https://www.plantuml.com/plantuml".
        disable_ssl_certificate_validation (bool): Whether to disable SSL certificate validation. Default is False.
        bin_path (str): The path to the PlantUML binary. Default is "/usr/local/bin/plantuml".
        output_format (str): The output format for generated diagrams [png|svg|txt|utxt|vdx|latex:nopreamble|latex|pdf]. Default is "png".
        allow_multiple_roots (bool): Whether to allow multiple diagram roots. Default is False.
        diagram_root (str): The root directory for diagram files. Default is "docs/diagrams".
        output_folder (str): The output folder for generated diagrams. Default is "out".
        output_in_dir (bool): Whether to output diagrams in separate directories. Default is False.
        input_folder (str): The input folder for diagram source files. Default is "src".
        input_extensions (str): The extensions of diagram source files. Default is an empty string.
        theme_enabled (bool): Whether to enable custom themes for diagrams. Default is False.
        theme_folder (str): The folder containing custom theme files. Default is "include/themes/".
        theme_light (str): The filename of the light theme file. Default is "light.puml".
        theme_dark (str): The filename of the dark theme file. Default is "dark.puml".
        prettify_svg (bool): Whether to pretty print the svg xml content before saving it to a file. Default is False.
    """
    render = mkdocs.config.config_options.Type(str, default="server")
    server = mkdocs.config.config_options.Type(
        str, default="https://www.plantuml.com/plantuml"
    )
    disable_ssl_certificate_validation = mkdocs.config.config_options.Type(
        bool, default=False
    )
    bin_path = mkdocs.config.config_options.Type(str, default="/usr/local/bin/plantuml")
    output_format = mkdocs.config.config_options.Type(str, default="png")
    allow_multiple_roots = mkdocs.config.config_options.Type(bool, default=False)
    diagram_root = mkdocs.config.config_options.Type(str, default="docs/diagrams")
    output_folder = mkdocs.config.config_options.Type(str, default="out")
    output_in_dir = mkdocs.config.config_options.Type(bool, default=False)
    input_folder = mkdocs.config.config_options.Type(str, default="src")
    input_extensions = mkdocs.config.config_options.Type(str, default="")
    theme_enabled = mkdocs.config.config_options.Type(bool, default=False)
    theme_folder = mkdocs.config.config_options.Type(str, default="include/themes/")
    theme_light = mkdocs.config.config_options.Type(str, default="light.puml")
    theme_dark = mkdocs.config.config_options.Type(str, default="dark.puml")
    prettify_svg = mkdocs.config.config_options.Type(bool, default=False)


class BuildPlantumlPlugin(BasePlugin[BuildPlantumlPluginConfig]):
    """
    A plugin for building PlantUML diagrams during the MkDocs build process.

    This plugin is responsible for processing PlantUML diagrams and generating the corresponding output files.
    It searches for diagram files in the specified directories, reads the source files, and converts them to the
    desired output format. It also supports dark mode and includes functionality for handling include statements.

    The plugin provides a pre-build hook that is called before the build process starts. During this hook, it checks
    the configuration parameters and looks for diagram files to process. It then performs the necessary operations
    to generate the output files for each diagram.

    """

    def __init__(self):
        self.total_time = 0

    def on_pre_build(self, config):
        """
        Pre-build hook for the plugin.

        This function is called before the build process starts. It checks the given parameters and looks for files
        to process.

        Args:
            config: The configuration object for the plugin.

        Returns:
            The updated configuration object.

        """
        diagram_roots = []

        if self.config["allow_multiple_roots"]:
            # Run through cwd in search of diagram roots
            for subdir, dirs, _ in Path.cwd().walk():
                for directory in dirs:
                    my_subdir = f"{subdir}/{directory}"
                    if my_subdir.endswith(self.config["diagram_root"]):
                        diagram_roots.append(self._make_diagram_root(my_subdir))
        else:
            diagram_roots.append(self._make_diagram_root(self.config["diagram_root"]))

        # Run through input folders
        for root in diagram_roots:
            for subdir, _, files in Path(root.src_dir).walk():
                for file in files:
                    if self._file_matches_extension(file):
                        diagram = PuElement(file, subdir)
                        diagram.root_dir = root.root_dir
                        diagram.out_dir = self._get_out_directory(root, subdir)

                        # Handle to read source file
                        with (Path(diagram.directory) / diagram.file).open("rt", encoding="utf-8") as f:
                            diagram.src_file = f.readlines()

                        # Search for start (@startuml <filename>)
                        if not self._search_start_tag(diagram):
                            # check the outfile (.ext will be set to .png or .svg etc.)
                            self._build_out_filename(diagram)

                        # Checks modification times for target and include files to know if we update
                        self._build_mtimes(diagram)

                        # Go through the file (only relevant for server rendering)
                        self._readFile(diagram, False)

                        # Finally convert
                        self._convert(diagram)

                        # Go through the file a second time for themed option
                        self._readFile(diagram, True)

                        # Finally convert
                        self._convert(diagram, True)

        return config

    def _make_diagram_root(self, subdir):
            """
            Creates a DiagramRoot object with the specified subdirectory.

            Args:
                subdir (str): The subdirectory for the diagram root.

            Returns:
                DiagramRoot: The created DiagramRoot object.
            """
            diagram_root = DiagramRoot()
            diagram_root.root_dir = str(Path.cwd() / subdir)
            diagram_root.src_dir = str(Path.cwd() / subdir / self.config["input_folder"])
            print(
                "root dir: {}, src dir: {}".format(
                    diagram_root.root_dir, diagram_root.src_dir
                )
            )
            return diagram_root

    def _get_out_directory(self, root, subdir):
        """
        Get the output directory for the generated files.

        Args:
            root (RootConfig): The root configuration object.
            subdir (str): The subdirectory path.

        Returns:
            str: The output directory path.

        """
        relPath = Path(subdir)
        try:
            relPath = relPath.relative_to(root.src_dir)
        except ValueError as ve:
            pass
        if self.config["output_in_dir"]:
            return str(Path.cwd() / root.root_dir
                       / relPath
                       / self.config["output_folder"])
        else:
            return str(Path.cwd() / root.root_dir
                       / self.config["output_folder"]
                       / relPath)

    def _search_start_tag(self, diagram):
        """
        Searches for the start tag in the given diagram's source file and sets the output file paths accordingly.

        Args:
            diagram (Diagram): The diagram object containing the source file and output directory.

        Returns:
            bool: True if the start tag is found and the output file paths are set, False otherwise.
        """
        outDir = Path(diagram.out_dir)
        start_tag = "@startuml"

        for line in diagram.src_file:
            line = line.strip()
            if line.startswith(start_tag):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    out_filename = parts[1]
                    diagram.out_file = str(outDir / f"{out_filename}.{self.config['output_format']}")
                    diagram.out_file_dark = str(outDir / f"{out_filename}_dark.{self.config['output_format']}")
                    return True
        return False

    def _build_mtimes(self, diagram):
        """
        Builds the modification times for the diagram files.

        Args:
            diagram (Diagram): The diagram object.

        Returns:
            None
        """
        # Compare the file mtimes between src and target
        try:
            diagram.img_time = Path(diagram.out_file).stat().st_mtime
        except Exception:
            diagram.img_time = 0

        try:
            diagram.img_time_dark = Path(diagram.out_file_dark).stat().st_mtime
        except Exception:
            diagram.img_time_dark = 0

        diagram.src_time = (Path(diagram.directory) / diagram.file).stat().st_mtime

        # Include time
        diagram.inc_time = 0

    def preprocess_includes(self, lines):
        include_lines = []
        for i, line in enumerate(lines):
            if re.match(r"^!include", line.strip()):
                include_lines.append((i, line.strip()))
        return include_lines

    def process_includes(self, diagram, include_lines, lines, directory, dark_mode):
        for i, line in include_lines:
            include_content = self._readIncludeLine(diagram, line, '', directory, dark_mode)
            lines[i] = include_content

    def _readFile(self, diagram, dark_mode):
        """
        Reads the contents of a file and performs compression, base64 encoding, and other operations on it.

        Args:
            diagram (Diagram): The diagram object containing information about the file.
            dark_mode (bool): A flag indicating whether dark mode is enabled.

        Returns:
            None
        """
        print(f"Processing diagram {diagram.file}")
        
        # Read the file into a list of lines
        lines = diagram.src_file.copy()
        
        # Process the file recursively
        temp_file = self._readFileRecursively(
            lines, diagram, diagram.directory, dark_mode
        )
        
        try:
            compressed_str = zlib.compress(temp_file.encode("utf-8"))
            compressed_string = compressed_str[2:-4]
            diagram.b64encoded = (
                base64.b64encode(compressed_string)
                .translate(b64_to_plantuml)
                .decode("utf-8")
            )
            diagram.concat_file = temp_file
        except Exception as e:
            print(f"Error during compression and encoding: {e}")
            diagram.b64encoded = ""

    def _readFileRecursively(self, lines, diagram, directory, dark_mode):
        """
        Processes the lines of a PlantUML file, handling !include directives and other content.

        Args:
            lines (list): The lines of the PlantUML file.
            diagram (Diagram): The diagram object containing metadata.
            directory (str): The directory containing the PlantUML file.
            dark_mode (bool): Indicates whether to process the diagram in dark mode.

        Returns:
            str: The processed PlantUML content.
        """
        include_lines = self.preprocess_includes(lines)
        self.process_includes(diagram, include_lines, lines, directory, dark_mode)
        return ''.join(lines)

    def _readIncludeLine(self, diagram, line, temp_file, directory, dark_mode):
        """
        Handles the different include types like !includeurl, !include, and !includesub.
        """
        if re.match(r"^!includeurl\s+\S+\s*$", line):
            temp_file += line

        elif re.match(r"^!includesub\s+\S+\s*$", line):
            parts = line[11:].strip().split("!")
            if len(parts) == 2:
                inc_file = parts[0]
                sub_name = parts[1]

                if dark_mode:
                    inc_file = inc_file.replace(
                        self.config["theme_light"], self.config["theme_dark"]
                    )

                try:
                    inc_file_abs = str((Path(directory) / inc_file).resolve())
                    temp_file = self._read_incl_sub(diagram, temp_file, dark_mode, inc_file_abs, sub_name)
                except Exception as e1:
                    try:
                        inc_file_abs = str((Path(diagram.root_dir) / inc_file).resolve())
                        temp_file = self._read_incl_sub(diagram, temp_file, dark_mode, inc_file_abs, sub_name)
                    except Exception as e2:
                        print("Could not find included file" + str(e1) + str(e2))
                        raise e2
            else:
                raise Exception(
                    "Invalid !includesub syntax. Expected: !includesub <filepath>!<sub_name>"
                )

        elif re.match(r"^!include\s+\S+\s*$", line):
            inc_file = line[9:].rstrip()

            if dark_mode:
                inc_file = inc_file.replace(
                    self.config["theme_light"], self.config["theme_dark"]
                )

            if inc_file.startswith("http") or inc_file.startswith("<"):
                temp_file += line
                return temp_file

            try:
                inc_file_abs = (Path(directory) / inc_file).resolve()
                if inc_file_abs.exists():
                    temp_file = self._read_incl_line_file(diagram, temp_file, dark_mode, inc_file_abs)
                else:
                    print(f"Could not find include in primary location: {inc_file_abs}")
                    inc_file_abs_alt = (Path(diagram.root_dir) / inc_file).resolve()
                    if inc_file_abs_alt.exists():
                        temp_file = self._read_incl_line_file(diagram, temp_file, dark_mode, inc_file_abs_alt)
                    else:
                        print(f"Could not find include in secondary location: {inc_file_abs_alt}")
                        raise Exception(f"Include could not be resolved: {line}")
            except FileNotFoundError as fnfe:
                print(f"Could not find include {fnfe}")
        else:
            raise Exception(f"Unknown include type: {line}")
        return temp_file
    
    def _read_incl_line_file(self, diagram, temp_file, dark_mode, inc_file_abs):
        """
        Read the included line file and update the diagram's inc_time if necessary.

        Args:
            diagram (Diagram): The diagram object to update.
            temp_file (str): The temporary file to write the included lines.
            dark_mode (bool): Flag indicating whether to use dark mode.
            inc_file_abs (Path): The absolute path of the included line file.

        Returns:
            str: The updated temporary file.
        """
        try:
            local_inc_time = inc_file_abs.stat().st_mtime
        except Exception as _:
            local_inc_time = 0

        if local_inc_time > diagram.inc_time:
            diagram.inc_time = local_inc_time

        with inc_file_abs.open("rt") as inc:
            lines = inc.readlines()
            temp_file = self._readFileRecursively(
                lines,
                diagram,
                inc_file_abs.parent.resolve(),
                dark_mode,
            )

        return temp_file

    def _read_incl_sub(self, diagram, temp_file, dark_mode, inc_file_abs, inc_sub_name):
        """
        Handle !includesub statements.

        Args:
            diagram (Diagram): The diagram object.
            temp_file (str): The temporary file path.
            dark_mode (bool): Flag indicating whether dark mode is enabled.
            inc_file_abs (str): The absolute path of the included file.
            inc_sub_name (str): The name of the included sub.

        Returns:
            str: The updated temporary file path.
        """
        # Save the mtime of the inc file to compare
        incFileAbs = Path(inc_file_abs)
        try:
            local_inc_time = incFileAbs.stat().st_mtime
        except Exception as _:
            local_inc_time = 0

        if local_inc_time > diagram.inc_time:
            diagram.inc_time = local_inc_time

        temp_sub = []
        add_following = False
        with incFileAbs.open("rt") as inc:
            for line in inc:
                line = line.strip()
                if re.match(r"^!startsub\s+" + re.escape(inc_sub_name) + r"\s*$", line):
                    add_following = True
                elif re.match(r"^!endsub\s*$", line) or re.match(r"^@enduml\s*$", line):
                    add_following = False
                elif add_following:
                    temp_sub.append(line)

            temp_file = self._readFileRecursively(
                temp_sub,  # Do only use the subs for further recursion
                temp_file,
                diagram,
                incFileAbs.parent.resolve(),
                dark_mode,
            )

        return temp_file

    def _build_out_filename(self, diagram):
            """
            Builds the output filename for the given diagram.

            Args:
                diagram (Diagram): The diagram object.

            Returns:
                Diagram: The updated diagram object with the output filename set.
            """
            out_index = diagram.file.rfind(".")
            if out_index > -1:
                diagram.out_file = (
                    diagram.file[: out_index + 1] + self.config["output_format"]
                )
                diagram.out_file_dark = (
                    diagram.file[:out_index] + "_dark." + self.config["output_format"]
                )

            diagram.out_file = str(Path(diagram.out_dir) / diagram.out_file)
            diagram.out_file_dark = str(Path(diagram.out_dir) / diagram.out_file_dark)

            return diagram

    def _convert(self, diagram, dark_mode=False):
        """
        Converts the given diagram to an image using either local rendering or server rendering.

        Args:
            diagram (Diagram): The diagram object to convert.
            dark_mode (bool, optional): Indicates whether to convert the diagram in dark mode. Defaults to False.
        """
        diagramFile = Path(diagram.directory) / diagram.file
        
        # Determine if conversion is needed
        if not dark_mode:
            needs_conversion = (
                (diagram.img_time < diagram.src_time) or 
                (diagram.inc_time > diagram.img_time)
            )
        else:
            needs_conversion = (
                (diagram.img_time_dark < diagram.src_time) or 
                (diagram.inc_time > diagram.img_time_dark)
            )

        if needs_conversion:
            print(f"Converting {diagramFile}")

            if self.config["render"] == "local":
                print(f"Converting Locally")
                command = self.config["bin_path"].rsplit()
                cmd_args = [
                    *command,
                    "-t" + self.config["output_format"]
                ]

                if self.config.get("theme_enabled", False):
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".puml", mode='w', encoding='utf-8') as temp_puml:
                        temp_puml.write(diagram.concat_file)
                        temp_puml_path = temp_puml.name
                    cmd_args.append(temp_puml_path)
                else:
                    cmd_args.append(str(diagramFile))

                if dark_mode:
                    cmd_args.append("-darkmode")

                with tempfile.TemporaryDirectory() as tmpdirname:
                    tmp_output_dir = Path(tmpdirname)
                    cmd_args.extend(["-o", str(tmp_output_dir)])

                    call(cmd_args)

                    # List files in the temporary directory
                    generated_files = list(tmp_output_dir.glob('*'))

                    if not generated_files:
                        raise RuntimeError("No files were generated by PlantUML")

                    generated_file_path = generated_files[0]

                    # Move and rename the generated file
                    if dark_mode:
                        shutil.move(str(generated_file_path), diagram.out_file_dark)
                        output_file = diagram.out_file_dark
                    else:
                        shutil.move(str(generated_file_path), diagram.out_file)
                        output_file = diagram.out_file

                    # Format the SVG content if the output format is SVG
                    if self.config["output_format"] == "svg":
                        try:
                            with open(output_file, "r", encoding="utf-8") as file:
                                svg_content = file.read()
                            formatted_svg = self._pretty_print_svg(svg_content)
                            with open(output_file, "w", encoding="utf-8") as file:
                                file.write(formatted_svg)
                            print("Formatting SVG content successful.")
                        except Exception as e:
                            print(f"Error formatting SVG content: {e}")

            else:
                print(f"Converting with PlantUML Server")
                content = self._call_server(diagram, diagram.out_file_dark if dark_mode else diagram.out_file)

                # Format the SVG content if the output format is SVG
                if self.config["output_format"] == "svg":
                    try:
                        formatted_svg = self._pretty_print_svg(content.decode("utf-8"))
                        print("Formatting SVG content successful.")
                    except Exception as e:
                        print(f"Error formatting SVG content: {e}")
                        formatted_svg = content.decode("utf-8")  # Fallback to raw content
                else:
                    formatted_svg = content.decode("utf-8")

                output_file = diagram.out_file_dark if dark_mode else diagram.out_file
                with open(output_file, "w", encoding="utf-8") as file:
                    file.write(formatted_svg)


    def _call_server(self, diagram, out_file):
        """
        Calls the PlantUML server to process the given diagram and save the output to a file.

        Args:
            diagram (Diagram): The diagram object containing the diagram data.
            out_file (str): The name of the output file.

        Raises:
            Exception: If there is a server error while processing the diagram.

        Returns:
            None
        """
        http = httplib2.Http({})

        if self.config["disable_ssl_certificate_validation"]:
            http.disable_ssl_certificate_validation = True

        url = (
            self.config["server"]
            + "/"
            + self.config["output_format"]
            + "/"
            + diagram.b64encoded
        )

        print(f"Making request to URL: {url}")

        try:
            response, content = http.request(url)
            print(f"Response status: {response.status}")
            if response.status != 200:
                print(f"Wrong response status for {diagram.file}: {response.status}")
                return
        except Exception as error:
            print(f"Server error while processing {diagram.file}: {error}")
            raise error
        else:
            outDir = Path(diagram.out_dir)
            outDir.mkdir(parents=True, exist_ok=True)

            # Format the SVG content if the output format is SVG
            if self.config["output_format"] == "svg":
                try:
                    formatted_svg = self._pretty_print_svg(content.decode("utf-8"))
                    print("Formatting SVG content successful.")
                except Exception as e:
                    print(f"Error formatting SVG content: {e}")
                    formatted_svg = content.decode("utf-8")  # Fallback to raw content
            else:
                formatted_svg = content.decode("utf-8")

            output_path = outDir / out_file
            print(f"Saving output to {output_path}")
            try:
                with output_path.open("w", encoding="utf-8") as out:
                    out.write(formatted_svg)
                    print("File saved successfully.")
            except Exception as e:
                print(f"Error saving file: {e}")
                raise

    def _pretty_print_svg(self, svg_content):
        """
        Pretty prints the SVG content using ElementTree.

        Args:
            svg_content (str): The SVG content.

        Returns:
            str: The pretty printed SVG content.
        """
        try:
            root = etree.fromstring(svg_content)
            pretty_xml = etree.tostring(root, pretty_print=True, encoding='utf-8').decode('utf-8')
            return pretty_xml
        except Exception as e:
            print(f"Error pretty-printing SVG content: {e}")
            return svg_content  # Fallback to raw content if pretty-printing fails


    def _file_matches_extension(self, file):
            """
            Check if the given file matches any of the input extensions specified in the configuration.

            Args:
                file (str): The file name to check.

            Returns:
                bool: True if the file matches any of the input extensions, False otherwise.
            """
            if len(self.config["input_extensions"]) == 0:
                return True
            extensions = self.config["input_extensions"].split(",")
            for extension in extensions:
                if file.endswith(extension):
                    return True
            return False


class PuElement:
    """plantuml helper object"""

    def __init__(self, file, subdir):
        self.file = file
        self.directory = subdir
        self.out_dir = ""
        self.root_dir = ""
        self.img_time = 0
        self.img_time_dark = 0
        self.inc_time = 0
        self.src_time = 0
        self.out_file = ""
        self.out_file_dark = ""
        self.b64encoded = ""
        self.concat_file = ""
        self.src_file = ""


class DiagramRoot:
    """object containing the src and out directories per diagram root"""

    def __init__(self):
        self.root_dir = ""
        self.src_dir = ""
