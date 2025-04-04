// Licensed to Cloudera, Inc. under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  Cloudera, Inc. licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { hueWindow } from 'types/types';

const changeURL = (
  newURL: string,
  params?: Record<string, string | number | boolean>,
  isReplace?: boolean
): void => {
  let extraSearch = '';
  if (params) {
    const paramsToString = Object.fromEntries(
      Object.entries(params).map(([key, value]) => [key, String(value)])
    );
    extraSearch += new URLSearchParams(paramsToString).toString();
  }

  const hashSplit = newURL.split('#');
  const hueBaseUrl = (<hueWindow>window).HUE_BASE_URL;
  const base =
    hueBaseUrl && hashSplit[0].length && hashSplit[0].indexOf(hueBaseUrl) !== 0 ? hueBaseUrl : '';
  let newUrl = base + hashSplit[0];
  if (extraSearch) {
    newUrl += (newUrl.indexOf('?') === -1 ? '?' : '&') + extraSearch;
  }
  if (hashSplit.length > 1) {
    //the foldername may contain # , so create substring ignoring first #
    newUrl += '#' + newURL.substring(newURL.indexOf('#') + 1);
  } else if (window.location.hash) {
    newUrl += window.location.hash;
  }

  if (window.location.href === newUrl) {
    // If the URLs are the same, do nothing
    return;
  }

  if (isReplace) {
    window.history.replaceState(null, '', newUrl);
  } else {
    window.history.pushState(null, '', newUrl);
  }
};

export default changeURL;
