import os

from aiohttp import web
from aiostream import stream, pipe

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
import base64
import threading

from gidgethub import routing, sansio
from gidgethub import aiohttp as gh_aiohttp
import asyncio

router = routing.Router()
username = "simoncozens"

secret = os.environ.get("GH_SECRET")
oauth_token = os.environ.get("GH_AUTH")


async def shape_this_blob(string, blob, commit):
    with tempfile.NamedTemporaryFile() as ntf:
        ntf.write(blob)
        vh = Vharfbuzz(ntf.name)
        buf = vh.shape(string)
        serialized = vh.serialize_buf(buf)
        s = {
            "commit": commit,
            "version": FontVersion(ntf.name).get_version_number_string(),
            "shaping": serialized,
            "svg": vh.buf_to_svg(buf),
        }

        svg = s["svg"].replace('transform="matrix(1 0 0 -1 0 0)"', "")
        png = cairosvg.svg2png(bytestring=svg)
        image = Image.open(io.BytesIO(png))
        imageBox = image.getbbox()
        cropped = image.crop(imageBox)
        cropped = ImageOps.flip(cropped)
        new_image = Image.new("RGBA", cropped.size, "WHITE")
        new_image.paste(cropped, (0, 0), cropped)
        new_image.thumbnail((600, 400), Image.ANTIALIAS)
        img_byte_arr = io.BytesIO()
        new_image.save(img_byte_arr, format="PNG")
        upload = cloudinary.uploader.upload(img_byte_arr.getvalue())
        s["url"] = upload["url"]
    return s


async def all_versions(gh, path, text):
    shas = [
        (text, path, x["sha"])
        for x in await gh.getitem(
            "https://api.github.com/repos/googlefonts/noto-fonts/commits?path=" + path
        )
    ]
    if len(shas) > 10:
        shas = shas[0:10]
    ghs = stream.repeat(gh)
    xs = stream.zip(ghs, stream.iterate(shas))
    ys = stream.starmap(xs, get_version, ordered=True)
    return await stream.list(stream.starmap(ys, shape_this_blob))


async def get_version(gh, pair):
    text, path, version = pair
    cache_key = "/tmp/cache_" + path.replace("/", "_") + "-" + version
    if os.path.isfile(cache_key):
        return text, open(cache_key, "rb").read(), version
    print(f"Downloading {path}@{version}")
    default_tree = await gh.getitem(
        f"https://api.github.com/repos/googlefonts/noto-fonts/git/trees/{version}?recursive=1"
    )
    tree_entry = [t for t in default_tree["tree"] if t["path"] == path][0]
    data = (await gh.getitem(tree_entry["url"]))["content"]
    binary = base64.b64decode(data)
    with open(cache_key, "wb") as f:
        f.write(binary)
    return text, binary, version


async def answer_question(gh, question):
    if not "@notobot" in question:
        print("Not for me!")
        return
    m = re.search("regression test (.*) with (.*)", question)
    if not m:
        print(f"Couldn't parse question |{question}|!")
        return
    string, file = m[1], m[2]

    if file.startswith("/"):
        file = file[1:]
    if "hinted" not in file:
        file = "unhinted/ttf/" + file

    print("Getting %s" % file)
    shaped = await all_versions(gh, file, string)
    if not shaped:
        return
    message = "Here's your regression log:\n\n"

    for ix, s in enumerate(shaped):
        message += (
            f"## {os.path.basename(file)} {s['version']} @ {s['commit'][0:7]}\n\n"
        )
        message += f"`{s['shaping']}`\n\n"
        if "url" in s:
            message += f"<img src=\"{s['url']}\">\n\n"
    return message


# print(
#     answer_question("@notobot, regression test سبے with NotoNastaliqUrdu-Regular.ttf")
# )

routes = web.RouteTableDef()


@router.register("issue_comment")
async def issue_comment_event(event, gh, *args, **kwargs):
    print("Issue comment created")
    print(event.data)
    url = event.data["issue"]["comments_url"]
    message = await answer_question(gh, event.data["comment"]["body"])
    if message:
        await gh.post(url, data={"body": message})
    pass


@routes.post("/")
async def main(request):
    # read the GitHub webhook payload
    body = await request.read()
    print("Got a thing:")

    # our authentication token and secret
    secret = os.environ.get("GH_SECRET")
    oauth_token = os.environ.get("GH_AUTH")

    # a representation of GitHub webhook event
    event = sansio.Event.from_http(request.headers, body, secret=secret)
    print(body)

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
