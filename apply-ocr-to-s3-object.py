#!/usr/bin/env python3
import os
import json
import urllib.parse
import boto3
import ocrmypdf
import uuid
import pdf2image

print('Loading function')

def apply_ocr_to_document_handler(event, context):
    print("Received event: " + json.dumps(event, indent=2))

    # Get the object from the event and show its content type

    region_name = event.get('awsRegion')
    if region_name:
        s3 = boto3.client('s3', region_name=region_name)

        bucket = event.get('s3', {}).get('bucket', {}).get('name')
        key = urllib.parse.unquote_plus(event.get('s3', {}).get('object', {}).get('key', ''), encoding='utf-8')
        if bucket and key != '':
            pages = event.get('pages')
            do_backup = event.get('doBackup')
            uuidstr = str(uuid.uuid1())
            try:
                inputname = '/tmp/input' + uuidstr + '.pdf'
                outputname = '/tmp/output' + uuidstr + '.pdf'
                s3.download_file(Bucket=bucket, Key=key, Filename=inputname)
                try:
                    ocrmypdf.ocr(inputname, outputname, pages=pages, force_ocr=True)
                except ocrmypdf.exceptions.EncryptedPdfError as epe:
                    print('PDF is encrypted. Attempting to rasterize images and retry.')
                    images = pdf2image.convert_from_path(inputname)
                    _inputname = inputname.replace('input', '_input')
                    if len(images) > 1:
                        images[0].save(_inputname, save_all=True, append_images=images[1:])
                    else:
                        images[0].save(_inputname)
                    ocrmypdf.ocr(_inputname, outputname, pages=pages, force_ocr=True)

                if do_backup:
                    s3.upload_file(inputname, bucket, key + '.bak')
                s3.upload_file(outputname, bucket, key)
                os.remove(inputname)
                os.remove(outputname)
                return
            except Exception as e:
                print(e)
                raise e
