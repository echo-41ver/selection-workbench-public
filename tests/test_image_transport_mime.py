import io
import pytest
from PIL import Image
from selection_workflow.bath_towel_deepseek import _api_image_bytes as towel_image
from selection_workflow.shower_openai_responses import _api_image_bytes as shower_image
from selection_workflow.api_image_input import ImageInputError

@pytest.mark.parametrize('encode',[towel_image,shower_image])
@pytest.mark.parametrize('format,mime',[('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp')])
def test_extensionless_cache_preserves_actual_image_format(tmp_path,encode,format,mime):
    path=tmp_path/'content.img'
    Image.new('RGB',(37,29),'red').save(path,format=format)
    original=path.read_bytes()
    payload,declared=encode(path)
    assert payload==original
    assert declared==mime

@pytest.mark.parametrize('encode',[towel_image,shower_image])
def test_actual_format_overrides_misleading_suffix(tmp_path,encode):
    path=tmp_path/'picture.jpg'
    Image.new('RGBA',(41,33),(0,255,0,127)).save(path,format='PNG')
    payload,mime=encode(path)
    assert payload==path.read_bytes() and mime=='image/png'

@pytest.mark.parametrize('encode',[towel_image,shower_image])
def test_large_png_still_uses_existing_whole_image_jpeg_encoding(tmp_path,encode):
    path=tmp_path/'large.img'
    Image.new('RGB',(2000,1000),'blue').save(path,format='PNG')
    payload,mime=encode(path)
    with Image.open(io.BytesIO(payload)) as sent:
        assert sent.size==(1600,800) and sent.format=='JPEG'
    assert mime=='image/jpeg'

@pytest.mark.parametrize('encode',[towel_image,shower_image])
@pytest.mark.parametrize('raw',[b'',b'not an image'])
def test_corrupt_image_rejected_locally(tmp_path,encode,raw):
    path=tmp_path/'bad.img';path.write_bytes(raw)
    with pytest.raises(ImageInputError,match='IMAGE_DECODE_FAILED'):
        encode(path)

@pytest.mark.parametrize('encode',[towel_image,shower_image])
@pytest.mark.parametrize('format',['TIFF','BMP','GIF'])
def test_valid_but_unapproved_format_not_silently_sent(tmp_path,encode,format):
    path=tmp_path/'unapproved.img';Image.new('RGB',(32,32),'blue').save(path,format=format)
    with pytest.raises(ImageInputError,match='IMAGE_FORMAT_UNSUPPORTED'):
        encode(path)

@pytest.mark.parametrize('encode',[towel_image,shower_image])
def test_parseable_but_truncated_png_rejected_before_send(tmp_path,encode):
    path=tmp_path/'truncated.img';Image.new('RGB',(40,40),'blue').save(path,format='PNG')
    path.write_bytes(path.read_bytes()[:45])
    with pytest.raises(ImageInputError,match='IMAGE_DECODE_FAILED'):
        encode(path)
