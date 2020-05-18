#!/usr/bin/env python3

import argparse
import hashlib
import tempfile
import zipfile
from logging import Logger, basicConfig, getLogger
from os import environ, getenv, path
from pathlib import Path
from struct import pack

import colorlog
import grpc
import toml
from minio import Minio

from generate import Problem, find_problem_dir
from scripts import library_checker_pb2 as libpb
from scripts import library_checker_pb2_grpc

logger: Logger = getLogger(__name__)

if __name__ == "__main__":
    handler = colorlog.StreamHandler()
    formatter = colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'white',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        })
    handler.setFormatter(formatter)
    basicConfig(
        level=getenv('LOG_LEVEL', 'INFO'),
        handlers=[handler]
    )

    parser = argparse.ArgumentParser(description='Testcase Deploy')
    parser.add_argument('-p', '--problem', nargs='*',
                        help='Generate problem', default=[])
    parser.add_argument('--host', default='localhost:50051', help='Host URL')
    parser.add_argument('--prod', action='store_true',
                        help='Production Mode(use SSL)')

    args = parser.parse_args()
    libdir = Path(__file__).parent
    tomls = []
    for problem_name in args.problem:
        problem_dir = find_problem_dir(libdir, problem_name)
        if problem_dir is None:
            logger.error('Cannot find problem: {}'.format(problem_name))
            raise ValueError('Cannot find problem: {}'.format(problem_name))
        tomls.append(problem_dir / 'info.toml')
    if len(tomls) == 0:
        tomls = list(filter(lambda p: not p.match(
            'test/**/info.toml'), Path('.').glob('**/info.toml')))

    logger.info('connect to API {} ssl={}'.format(args.host, args.prod))
    if args.prod:
        channel = grpc.secure_channel(
            args.host, grpc.ssl_channel_credentials())
        stub = library_checker_pb2_grpc.LibraryCheckerServiceStub(channel)
    else:
        channel = grpc.secure_channel(args.host, grpc.local_channel_credentials())
        stub = library_checker_pb2_grpc.LibraryCheckerServiceStub(channel)

    api_password = environ.get('API_PASS', 'password')
    response = stub.Login(libpb.LoginRequest(
        name='upload', password=api_password))
    cred_token = grpc.access_token_call_credentials(response.token)

    logger.info('connect to ObjectStorage')
    minio_host = environ.get('MINIO_HOST', 'localhost:9000')
    minio_access_key = environ.get('MINIO_ACCESS_KEY', 'minio')
    minio_secret_key = environ.get('MINIO_SECRET_KEY', 'miniopass')

    minio_client = Minio(minio_host,
                        access_key=minio_access_key,
                        secret_key=minio_secret_key,
                        secure=args.prod)

    bucket_name = environ.get('MINIO_BUCKET', 'testcase')

    if not minio_client.bucket_exists(bucket_name):
        logger.error('No bucket {}'.format(bucket_name))
        raise ValueError('No bucket {}'.format(bucket_name))
        
    for toml_path in tomls:
        probdir = toml_path.parent
        name = probdir.name
        problem = Problem(libdir, probdir)
        problem.generate(problem.Mode.DEFAULT, None)

        title = problem.config['title']
        timelimit = problem.config['timelimit']

        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            m = hashlib.sha256()

            with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as newzip:
                def zip_write(filename, arcname):
                    newzip.write(filename, arcname)
                    m.update(pack('q', path.getsize(filename)))
                    with open(filename, 'rb') as f:
                        m.update(f.read())
                zip_write(probdir / 'checker.cpp', arcname='checker.cpp')
                for f in sorted(probdir.glob('in/*.in')):
                    zip_write(f, arcname=f.relative_to(probdir))
                for f in sorted(probdir.glob('out/*.out')):
                    zip_write(f, arcname=f.relative_to(probdir))

            tmp.seek(0)
            data = tmp.read()
            datahash = m.hexdigest()

            print('[*] deploy {} {}Mbytes {}'.format(name,
                                                        len(data) / 1024 / 1024, datahash))
            
            minio_client.fput_object(bucket_name, datahash + '.zip', tmp.name, part_size=5 * 1000 * 1000 * 1000)
            # convert task
            html = problem.gen_html()
            statement = html.statement

            stub.ChangeProblemInfo(libpb.ChangeProblemInfoRequest(
                name=name, title=title, statement=statement, time_limit=timelimit, case_version=datahash
            ), credentials=cred_token)
