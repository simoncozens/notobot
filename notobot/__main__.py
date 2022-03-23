import os

from aiohttp import web
import cairosvg
import pygit2
from fontv.libfv import FontVersion
from vharfbuzz import Vharfbuzz
import tempfile
import io
from PIL import Image, ImageOps
import cloudinary.uploader
import re
import aiohttp

from gidgethub import routing, sansio
from gidgethub import aiohttp as gh_aiohttp

router = routing.Router()
username = "simoncozens"

if not os.path.isdir("notofonts"):
    pygit2.clone_repository("https://github.com/googlefonts/noto-fonts", "notofonts")


def shape_all_versions(path, string):
    def shape_this_blob(versions, blob, commit):
        with tempfile.NamedTemporaryFile() as ntf:
            ntf.write(blob)
            vh = Vharfbuzz(ntf.name)
            buf = vh.shape(string)
            serialized = vh.serialize_buf(buf)
            versions.append(
                {
                    "commit": commit,
                    "version": FontVersion(ntf.name).get_version_number_string(),
                    "shaping": serialized,
                    "svg": vh.buf_to_svg(buf),
                }
            )

    repo = pygit2.Repository(os.path.join(os.getcwd(), "notofonts", ".git"))
    prev = None
    oldversion = ""
    versions = []
    try:
        for cur in repo.walk(repo.head.target):
            if prev is not None:
                commit = prev.id
                diff = cur.tree.diff_to_tree(prev.tree)
                for patch in diff:
                    if (
                        path in patch.delta.new_file.path
                        and "unhinted/ttf" in patch.delta.new_file.path
                    ):
                        blob = cur.tree[patch.delta.new_file.path].data
                        if blob == oldversion:
                            continue
                        shape_this_blob(versions, blob, commit)
                        oldversion = blob
            prev = cur
            if cur.parents:
                cur = cur.parents[0]
    except Exception as e:
        print(e)

    return versions


def answer_question(question):
    if not "@notobot" in question:
        return
    m = re.search("regression test (.*) with (.*)", question)
    if not m:
        return
    string, file = m[1], m[2]

    shaped = shape_all_versions(file, string)
    if not shaped:
        return
    message = "Here's your regression log:\n\n"

    for ix, s in enumerate(shaped):
        svg = s["svg"].replace('transform="matrix(1 0 0 -1 0 0)"', "")
        png = cairosvg.svg2png(bytestring=svg)
        image = Image.open(io.BytesIO(png))
        imageBox = image.getbbox()
        cropped = image.crop(imageBox)
        cropped = ImageOps.flip(cropped)
        new_image = Image.new("RGBA", cropped.size, "WHITE")
        new_image.paste(cropped, (0, 0), cropped)
        new_image.thumbnail((600, 400), Image.ANTIALIAS)
        new_image.save("img-%i.png" % ix)
        upload = cloudinary.uploader.upload("img-%i.png" % ix)
        s["url"] = upload["url"]
        message += f"## {file} {s['version']} @ {s['commit'].hex[0:7]}\n\n"
        message += f"`{s['shaping']}`\n\n"
        message += f"<img src=\"{s['url']}\">\n\n"
    return message


# print(
#     answer_question("@notobot, regression test سبے with NotoNastaliqUrdu-Regular.ttf")
# )

routes = web.RouteTableDef()


@router.register("issue_comment", action="created")
async def issue_comment_event(event, gh, *args, **kwargs):
    url = event.data["issue"]["comments_url"]
    message = answer_question(event.data["comment"]["body"])
    if message:
        await gh.post(url, data={"body": message})
    pass


@routes.post("/")
async def main(request):
    # read the GitHub webhook payload
    body = await request.read()

    # our authentication token and secret
    secret = os.environ.get("GH_SECRET")
    oauth_token = os.environ.get("GH_AUTH")

    # a representation of GitHub webhook event
    event = sansio.Event.from_http(request.headers, body, secret=secret)

    async with aiohttp.ClientSession() as session:
        gh = gh_aiohttp.GitHubAPI(session, username, oauth_token=oauth_token)

        # call the appropriate callback for the event
        await router.dispatch(event, gh)

    # return a "Success"
    return web.Response(status=200)


if __name__ == "__main__":
    app = web.Application()
    app.add_routes(routes)
    port = os.environ.get("PORT")
    if port is not None:
        port = int(port)

    web.run_app(app, port=port)
